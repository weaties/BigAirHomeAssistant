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


def _route(o: str, d: str, via: list[str], avoid: bool, alt: bool = False) -> dict[str, Any]:
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

    def route(o: str, d: str, via: list[str], avoid: bool, alt: bool) -> dict[str, Any]:
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


# --- slow mode's days to a far destination (#117) -----------------------------------


def _walmart_page(seg: object, token: object, query: str, kind: object) -> dict[str, Any]:
    assert query == "Walmart Supercenter" and kind is None
    return {
        "places": [
            {"displayName": {"text": "Walmart Supercenter"},
             "location": {"latitude": 42.5, "longitude": -111.0}},
            {"displayName": {"text": "Bob's Tires"},
             "location": {"latitude": 42.4, "longitude": -111.0}},
        ]
    }  # fmt: skip


def test_chain_plan_scores_stops_falls_back_to_walmart_and_caches_the_route() -> None:
    routes: list[tuple[str, str, bool]] = []
    searches: list[str] = []

    def route(o: str, d: str, via: list[str], avoid: bool, alt: bool) -> dict[str, Any]:
        routes.append((o, d, avoid))
        return _route(o, "44.0,-111.0", via, avoid)  # ~331 road mi at 45 mph: 2+ days

    def search(seg: object, token: object, query: str, kind: object) -> dict[str, Any]:
        searches.append(query)
        return _walmart_page(seg, token, query, kind)

    edges = _edges(
        route=route,
        search=search,
        pois=lambda: ([SPRINGS], []),
        ioverlander=lambda: ([PULLOUT], []),
    )
    req = ChainRequest("here", "Goal Town", layovers=(1,), utc_offset_min=-420)
    c = chain_plan(req, edges)
    assert routes == [("40.00000,-111.00000", "Goal Town", True)]
    # Day 1: Springs (a vault destination). The plain pullout has nothing near it.
    # Day 2: nothing worth stopping for in reach, so the Walmart. Day 3: the goal.
    assert [leg.kind for leg in c.chain.legs] == ["spot", "walmart", "goal"]
    assert c.chain.legs[0].to is not None and c.chain.legs[0].to.name == "Springs"
    assert [leg.depart.date().isoformat() for leg in c.chain.legs] == [
        "2027-05-10", "2027-05-12", "2027-05-13"
    ]  # fmt: skip
    assert c.chain.legs[0].depart.hour == 10

    j = chain_to_json(c)
    import json

    json.dumps(j)
    assert [leg["nights"] for leg in j["legs"]] == [2, 1, 0]
    assert j["legs"][1]["name"] == "Walmart Supercenter"
    assert j["legs"][1]["source"] == "walmart"
    assert j["max_day_mi"] == 300 and j["arrive_by"] == "15:00"
    assert j["arrive"].startswith("2027-05-13")

    # A layover change re-plans without paying for the route or the search again.
    n = len(searches)
    c2 = chain_plan(ChainRequest("here", "Goal Town", layovers=(0, 2), utc_offset_min=-420), edges)
    assert len(routes) == 1 and len(searches) == n
    assert [leg.depart.date().isoformat() for leg in c2.chain.legs] == [
        "2027-05-10", "2027-05-11", "2027-05-14"
    ]  # fmt: skip


def test_a_failed_walmart_search_warns_and_is_not_cached() -> None:
    def search(*a: object) -> dict[str, Any]:
        raise OSError("quota")

    edges = _edges(search=search, route=lambda o, d, v, a, alt: _route(o, "44.0,-111.0", v, a))
    c = chain_plan(ChainRequest("here", "Goal Town", utc_offset_min=-420), edges)
    assert any("Walmart search failed" in w for w in c.warnings)
    assert edges.chain_cache == {}
    assert c.chain.legs[0].kind == "open"


def test_chain_request_validation() -> None:
    req = chain_request_from_json(
        {"origin": "here", "goal": "Tucson", "layovers": [2, 0, "1"], "by": "2027-06-01"}
    )
    assert (req.goal, req.layovers) == ("Tucson", (2, 0, 1))
    assert req.by is not None and req.by.isoformat() == "2027-06-01"
    assert chain_request_from_json({"origin": "here", "goal": "Tucson"}).by is None
    utc = chain_request_from_json(
        {"origin": "here", "goal": "X", "depart": "2027-05-10T16:00:00Z", "utc_offset_min": -420}
    )
    assert utc.depart is not None and utc.depart.hour == 9
    with pytest.raises(TripError, match="destination"):
        chain_request_from_json({"origin": "here"})
    with pytest.raises(TripError, match="date"):
        chain_request_from_json({"origin": "here", "goal": "X", "by": "June"})
    with pytest.raises(TripError, match="negative"):
        chain_request_from_json({"origin": "here", "goal": "X", "layovers": [-1]})


def test_a_one_day_trip_skips_the_walmart_search() -> None:
    searches: list[object] = []
    edges = _edges(
        search=lambda *a: searches.append(a) or {},
        route=lambda o, d, v, a, alt: _route(o, "41.5,-111.0", v, a),  # ~124 road mi
    )
    c = chain_plan(ChainRequest("here", "Near Town", utc_offset_min=-420), edges)
    assert [leg.kind for leg in c.chain.legs] == ["goal"]
    assert searches == []
