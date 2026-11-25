"""Destination discovery orchestration (#91) with every edge faked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from bigair.destinations import Destination, crow_mi
from bigair.fuel_sources import encode_polyline
from bigair.poi import Poi
from bigair.trip_service import (
    ChainRequest,
    DiscoverRequest,
    TripEdges,
    TripError,
    chain_plan,
    chain_request_from_json,
    chain_to_json,
    discover,
    discover_request_from_json,
    discover_to_json,
)

NOW = datetime(2027, 5, 10, 15, 0, tzinfo=UTC)  # 08:00 PDT
DEPART = datetime(2027, 5, 10, 10, 0, tzinfo=timezone(timedelta(hours=-7)))  # 10:00 PDT
ORIGIN = (40.0, -111.0)


def _route(o: str, d: str, via: list[str], avoid: bool) -> dict[str, Any]:
    a = tuple(float(x) for x in o.split(","))
    b = tuple(float(x) for x in d.split(","))
    pts = [(a[0] + (b[0] - a[0]) * i / 10, a[1] + (b[1] - a[1]) * i / 10) for i in range(11)]
    road = crow_mi(a, b) * 1.2  # type: ignore[arg-type]
    return {
        "routes": [
            {
                "distanceMeters": road * 1609.344,
                "duration": f"{road / 45 * 3600:.0f}s",  # 45 mph off the interstates
                "polyline": {"encodedPolyline": encode_polyline(pts)},
            }
        ]
    }


def _edges(**over: object) -> TripEdges:
    base: dict[str, object] = {
        "route": _route,
        "search": lambda *a: {},
        "now": lambda: NOW,
        "pois": lambda: ([], []),
        "drive_s": lambda o, d: (20 * 60.0, 15.0),
        "ioverlander": lambda: ([], []),
        "public_land": lambda: ([], []),
        "position": lambda: ORIGIN,
    }
    base.update(over)
    return TripEdges(**base)  # type: ignore[arg-type]


SPRINGS = Poi("Springs", 41.0, -111.0, ("hot-springs",), "Soak.", 90, None, (5,), True, 5)
ROADSIDE = Poi("Roadside Arch", 40.5, -111.05, (), None, 30, None, None, None, 4)
PULLOUT = Destination("Mesa Pullout", 41.5, -111.0, "ioverlander", "Wild Camping", None)
BLM = Destination("Far BLM", 42.5, -111.0, "public_land", "BLM", None)


def test_discover_ranks_vault_destination_with_stops_first() -> None:
    edges = _edges(
        pois=lambda: ([SPRINGS, ROADSIDE], []),
        ioverlander=lambda: ([PULLOUT], []),
        public_land=lambda: ([BLM], []),
    )
    r = discover(DiscoverRequest("40.0,-111.0", depart=DEPART), edges)
    names = [o.dest.name for o in r.options]
    assert names[0] == "Springs"
    springs = r.options[0]
    # ★5 +1, in season (May) +0.5 = 2.5; plus the arch on the way: 1 + ★4 0.5 = 1.5.
    assert springs.dest_score == pytest.approx(2.5)
    assert springs.drive.chosen == ("Roadside Arch",)
    assert springs.total == pytest.approx(4.0)
    assert set(names) == {"Springs", "Mesa Pullout", "Far BLM"}
    assert all(o.drive.fits for o in r.options)


def test_discover_uses_campgrounds_only_when_nothing_dispersed_is_in_range() -> None:
    asked: list[tuple[float, float, float, float]] = []
    camp = Destination("Town KOA", 41.0, -111.2, "campground", None, None)

    def campgrounds(bbox: tuple[float, float, float, float]) -> list[Destination]:
        asked.append(bbox)
        return [camp]

    r = discover(DiscoverRequest("here", depart=DEPART), _edges(campgrounds=campgrounds))
    assert [o.dest.name for o in r.options] == ["Town KOA"]
    assert len(asked) == 1
    s, w, n, e = asked[0]
    assert s < ORIGIN[0] < n and w < ORIGIN[1] < e

    asked.clear()
    discover(
        DiscoverRequest("here", depart=DEPART),
        _edges(campgrounds=campgrounds, ioverlander=lambda: ([PULLOUT], [])),
    )
    assert asked == []  # a dispersed option exists, so no paid search


def test_discover_with_nothing_in_range_says_so() -> None:
    r = discover(DiscoverRequest("here", depart=DEPART), _edges())
    assert r.options == []
    assert any("No destinations" in w for w in r.warnings)


def test_discover_routes_at_most_the_cap() -> None:
    many = [
        Destination(f"Spot {i}", 40.2 + i * 0.1, -111.0, "ioverlander", None, None)
        for i in range(20)
    ]
    calls: list[str] = []

    def route(o: str, d: str, via: list[str], avoid: bool) -> dict[str, Any]:
        calls.append(d)
        return _route(o, d, via, avoid)

    discover(
        DiscoverRequest("here", depart=DEPART), _edges(route=route, ioverlander=lambda: (many, []))
    )
    from bigair.trip_service import MAX_ROUTED_DESTINATIONS

    assert len(calls) == MAX_ROUTED_DESTINATIONS


def test_discover_needs_gps_for_here() -> None:
    with pytest.raises(TripError, match="GPS"):
        discover(DiscoverRequest("here"), _edges(position=lambda: None))


def test_discover_json_and_request_parsing() -> None:
    edges = _edges(pois=lambda: ([SPRINGS], []))
    j = discover_to_json(discover(DiscoverRequest("40.0,-111.0", depart=DEPART), edges))
    import json

    json.dumps(j)
    o = j["options"][0]
    assert o["name"] == "Springs"
    assert o["source"] == "poi"
    assert o["fits"] is True
    assert o["lat"] == 41.0
    req = discover_request_from_json({"origin": "here", "utc_offset_min": -420})
    assert (req.origin, req.utc_offset_min) == ("here", -420)
    with pytest.raises(TripError):
        discover_request_from_json({})


def test_discover_reads_a_utc_departure_in_the_phones_time_zone() -> None:
    req = discover_request_from_json(
        {"origin": "here", "depart": "2027-05-10T17:00:00Z", "utc_offset_min": -420}
    )
    assert req.depart is not None
    assert req.depart.hour == 10
    r = discover(req, _edges(pois=lambda: ([SPRINGS], [])))
    assert r.options[0].drive.fits


# --- a chain of legs to a far goal (#92) -------------------------------------------


def test_chain_plan_sketches_legs_and_snaps_to_destinations() -> None:
    seen: list[tuple[str, str, bool]] = []

    def route(o: str, d: str, via: list[str], avoid: bool) -> dict[str, Any]:
        seen.append((o, d, avoid))
        return _route(o, "44.0,-111.0", via, avoid)  # ~276 crow mi → ~331 road mi

    edges = _edges(route=route, pois=lambda: ([SPRINGS], []), ioverlander=lambda: ([PULLOUT], []))
    req = ChainRequest("here", "Goal Town", by=datetime(2027, 5, 16).date(), utc_offset_min=-420)
    c = chain_plan(req, edges)
    assert seen == [("40.00000,-111.00000", "Goal Town", True)]
    # 6 days from May 10 → up to 3 legs of ~110 mi, well under the ceiling.
    assert len(c.chain.legs) == 3
    assert c.chain.legs[0].depart.isoformat() == "2027-05-10"
    j = chain_to_json(c)
    import json

    json.dumps(j)
    assert j["legs"][-1]["is_goal"] is True
    assert j["legs"][0]["name"]  # snapped or "Open spot"
    assert j["stay_nights"] == 3


def test_chain_request_validation() -> None:
    req = chain_request_from_json({"origin": "here", "goal": "Tucson", "by": "2027-06-01"})
    assert (req.goal, req.by.isoformat()) == ("Tucson", "2027-06-01")
    with pytest.raises(TripError, match="goal"):
        chain_request_from_json({"origin": "here", "by": "2027-06-01"})
    with pytest.raises(TripError, match="date"):
        chain_request_from_json({"origin": "here", "goal": "X", "by": "June"})
