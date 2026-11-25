"""Trip orchestration (#75) with every edge faked: no network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bigair.fuel_sources import LiveFuel, encode_polyline
from bigair.poi import Poi
from bigair.poi_near import Reach
from bigair.trip_service import (
    TripEdges,
    TripError,
    TripRequest,
    plan_trip,
    pois_for_cached,
    request_from_json,
    result_to_json,
    weather_summary,
)

NOW = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
FRESH = "2026-11-24T12:00:00Z"
POLY = encode_polyline([(40.0 + i * 0.1, -120.0) for i in range(21)])  # ~138 mi straight


def _route(*a: object) -> dict[str, Any]:
    return {
        "routes": [
            {"distanceMeters": 321869, "duration": "14400s", "polyline": {"encodedPolyline": POLY}}
        ]
    }


def _place(pid: str, name: str, lat: float, price: str) -> dict[str, Any]:
    units, _, cents = price.partition(".")
    return {
        "id": pid,
        "displayName": {"text": name},
        "location": {"latitude": lat, "longitude": -120.0},
        "formattedAddress": "1 Main St, Town, OR 97000, USA",
        "fuelOptions": {
            "fuelPrices": [
                {
                    "type": "DIESEL",
                    "price": {"currencyCode": "USD", "units": units, "nanos": int(cents) * 10**7},
                    "updateTime": FRESH,
                }
            ]
        },
    }


def _search(seg: object, tok: str | None, q: str, typ: str | None) -> dict[str, Any]:
    return {
        "places": [
            _place("a", "Shell", 40.5, "6.10"),
            _place("b", "Love's Travel Stop", 41.0, "6.40"),
            _place("c", "Chevron", 41.5, "6.90"),
        ]
    }


def _forecasts(points: list[tuple[float, float]]) -> list[dict[str, Any]]:
    times = [(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(72)]
    out = []
    for lat, _ in points:
        snowy = lat > 41.4
        out.append(
            {
                "elevation": 1200.0,
                "hourly": {
                    "time": times,
                    "temperature_2m": [-2.0 if snowy else 8.0] * 72,
                    "precipitation_probability": [80 if snowy else 0] * 72,
                    "precipitation": [1.0 if snowy else 0.0] * 72,
                    "snowfall": [1.2 if snowy else 0.0] * 72,
                    "weather_code": [73 if snowy else 1] * 72,
                    "wind_speed_10m": [5.0] * 72,
                    "wind_gusts_10m": [10.0] * 72,
                },
            }
        )
    return out


def _alerts(lat: float, lon: float) -> dict[str, Any]:
    if lat < 41.0:
        return {"features": []}
    return {
        "features": [
            {
                "properties": {
                    "id": "w1",
                    "event": "Winter Storm Warning",
                    "severity": "Severe",
                    "headline": "Winter Storm Warning for the pass",
                    "areaDesc": "Pass",
                    "onset": "2026-11-24T18:00:00Z",
                    "ends": "2026-11-25T18:00:00Z",
                }
            }
        ]
    }


def _edges(**over: object) -> TripEdges:
    base: dict[str, object] = {
        "route": _route,
        "search": _search,
        "now": lambda: NOW,
        "eia": lambda: {"response": {"data": []}},
        "forecasts": _forecasts,
        "alerts": _alerts,
        "fuel_state": lambda: LiveFuel(30.0, 15.0, True, False, False),
        "position": lambda: (47.6, -122.3),
    }
    base.update(over)
    return TripEdges(**base)  # type: ignore[arg-type]


def test_round_trip_plan_with_weather_and_alerts() -> None:
    r = plan_trip(TripRequest("A", "B", round_trip=True, reserves=(100.0, 50.0)), _edges())
    assert r.trip_mi == pytest.approx(2 * 200.0 + 20.0, abs=0.1)
    assert r.inp.calibrating
    assert r.inp.start_gal == 30.0
    assert len(r.options) == 2
    assert {p.leg for p in r.points} == {"out", "back"}
    assert len(r.hourly) == len([p for p in r.points if p.leg == "out"])
    assert [a.event for a in r.alerts] == ["Winter Storm Warning"]
    assert r.return_depart == NOW + timedelta(hours=6)  # 4 h drive + 2 h default
    lines = weather_summary(r)
    assert lines[0].startswith("Out:")
    assert "snow" in lines[0]
    assert "worst: Snow" in lines[0]
    assert lines[1].startswith("Back:")


def test_json_is_serialisable_and_has_map_data() -> None:
    r = plan_trip(TripRequest("A", "B", round_trip=True, reserves=(100.0,)), _edges())
    j = result_to_json(r)
    json.dumps(j)  # must not raise
    assert j["route"]["polyline"]
    stops = j["options"][0]["stops"]
    assert stops and all(s["lat"] is not None for s in stops)
    assert {s["leg"] for s in stops} <= {"out", "back"}
    assert j["weather"]["series"][0]["start"] == NOW.isoformat()
    assert j["weather"]["classify"]["severe_gust_mph"] == 45.0
    assert j["tank"]["calibrating"] is True


def test_current_location_uses_gps() -> None:
    seen: list[str] = []

    def route(o: str, d: str, via: list[str]) -> dict[str, Any]:
        seen.append(o)
        return _route()

    plan_trip(TripRequest("here", "B"), _edges(route=route))
    assert seen == ["47.60000,-122.30000"]


def test_current_location_without_fix_is_an_error() -> None:
    with pytest.raises(TripError, match="GPS"):
        plan_trip(TripRequest("here", "B"), _edges(position=lambda: None))


def test_stale_fuel_refuses() -> None:
    with pytest.raises(TripError, match="stale"):
        plan_trip(
            TripRequest("A", "B"), _edges(fuel_state=lambda: LiveFuel(30, 15, False, True, False))
        )


def test_no_fuel_state_needs_overrides() -> None:
    with pytest.raises(TripError, match="fuel state"):
        plan_trip(TripRequest("A", "B"), _edges(fuel_state=lambda: None))
    r = plan_trip(TripRequest("A", "B", tank_gal=40.0, mpg=14.0), _edges(fuel_state=lambda: None))
    assert r.inp.start_gal == 40.0


def test_no_route_is_an_error() -> None:
    with pytest.raises(TripError, match="no route"):
        plan_trip(TripRequest("A", "B"), _edges(route=lambda *a: {"routes": []}))


def test_weather_failure_is_only_a_warning() -> None:
    def boom(points: object) -> list[dict[str, Any]]:
        raise OSError("offline")

    r = plan_trip(TripRequest("A", "B"), _edges(forecasts=boom))
    assert r.options[0].feasible
    assert r.points == []
    assert any("Weather unavailable" in w for w in r.warnings)


def test_request_from_json_validates() -> None:
    req = request_from_json(
        {
            "origin": "Seattle, WA",
            "destination": "Sonoma Raceway",
            "via": ["", "Bend, OR"],
            "round_trip": True,
            "depart": "2026-11-28T08:00:00-08:00",
            "reserves": [150, "100"],
            "tank_gal": "",
        }
    )
    assert req.via == ("Bend, OR",)
    assert req.depart == datetime(2026, 11, 28, 16, 0, tzinfo=UTC)
    assert req.reserves == (150.0, 100.0)
    assert req.tank_gal is None
    with pytest.raises(TripError, match="start and a destination"):
        request_from_json({"origin": "A"})
    with pytest.raises(TripError, match="time zone"):
        request_from_json({"origin": "A", "destination": "B", "depart": "2026-11-28T08:00"})
    with pytest.raises(TripError, match="number"):
        request_from_json({"origin": "A", "destination": "B", "reserves": ["x"]})


# --- POIs near the route (#88) ---------------------------------------------------

# The fake route runs due north along lon -120 from lat 40.0 to 42.0.
NEAR = Poi("Near Springs", 41.0, -120.2, ("hot-springs",), "Warm.", 120, "Soak", (11, 12), True, 5)
FAR = Poi("Far Peak", 41.0, -124.0, ("geology",), None)


def _drive(origin: tuple[float, float], dest: tuple[float, float]) -> float:
    return 20 * 60.0


def _poi_edges(**over: object) -> TripEdges:
    base: dict[str, object] = {"pois": lambda: ([NEAR, FAR], []), "drive_s": _drive}
    base.update(over)
    return _edges(**base)


def test_plan_lists_pois_in_reach_with_detour_and_visit() -> None:
    r = plan_trip(TripRequest("A", "B", reach=Reach("detour", 3.0)), _poi_edges())
    assert r.pois_enabled
    assert [h.poi.name for h in r.pois] == ["Near Springs"]
    j = result_to_json(r)
    json.dumps(j)
    block = j["pois"]
    assert block["enabled"] is True
    assert block["reach"] == {"mode": "detour", "hours": 3.0}
    hit = block["hits"][0]
    assert hit["name"] == "Near Springs"
    assert hit["detour_min"] == 40
    assert hit["visit_min"] == 120
    assert hit["visit_what"] == "Soak"
    assert hit["status"] == "ok"
    assert hit["in_season"] is True  # November
    assert hit["rating"] == 5
    assert hit["destination"] is True
    assert hit["lat"] == 41.0
    assert hit["eta"]


def test_without_a_poi_folder_the_plan_has_no_poi_section() -> None:
    r = plan_trip(TripRequest("A", "B"), _edges())
    assert not r.pois_enabled
    assert result_to_json(r)["pois"]["enabled"] is False


def test_poi_folder_warnings_reach_the_plan() -> None:
    edges = _poi_edges(pois=lambda: ([], ["POI folder /poi not found; is Syncthing running?"]))
    r = plan_trip(TripRequest("A", "B"), edges)
    assert r.options[0].feasible  # the fuel plan stands without POIs
    assert "POI folder /poi not found; is Syncthing running?" in r.warnings


def test_request_from_json_reads_the_reach_selector() -> None:
    req = request_from_json(
        {"origin": "A", "destination": "B", "reach": {"mode": "each_way", "hours": "2"}}
    )
    assert req.reach == Reach("each_way", 2.0)
    assert request_from_json({"origin": "A", "destination": "B"}).reach == Reach("detour", 3.0)
    with pytest.raises(TripError, match="reach"):
        request_from_json({"origin": "A", "destination": "B", "reach": {"mode": "fly"}})


def test_changing_the_reach_reuses_the_cached_route_without_replanning() -> None:
    edges = _poi_edges()
    last = result_to_json(plan_trip(TripRequest("A", "B", reach=Reach("detour", 0.5)), edges))
    assert last["pois"]["hits"] == []  # 40 min round trip > 30 min

    def no_route(*a: object) -> dict[str, Any]:
        raise AssertionError("must not re-route")

    block = pois_for_cached(last, Reach("detour", 1.0), _poi_edges(route=no_route))
    assert [h["name"] for h in block["hits"]] == ["Near Springs"]
    assert block["reach"] == {"mode": "detour", "hours": 1.0}
