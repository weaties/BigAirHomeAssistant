"""POIs near a route, with the reach selector (#88). Drive times are faked."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bigair.fuel_sources import RouteInfo
from bigair.poi import Poi
from bigair.poi_near import (
    AHEAD_MAX_ONE_WAY_MIN,
    MAX_LOOKUPS,
    DriveTime,
    Reach,
    near_route,
    reach_from_json,
)

DEPART = datetime(2027, 5, 10, 17, 0, tzinfo=UTC)  # May

# A straight route due north along lon -110: lat 40.0 → 42.0 (~138 crow miles),
# routed as 150 road miles in 3 hours (so 50 mph, 1.2 min per road mile).
ROUTE = RouteInfo.from_points([(40.0 + i * 0.1, -110.0) for i in range(21)], 150.0, 3 * 3600.0)

# One degree of longitude at lat 41 is ~52.2 miles.


def poi(
    name: str,
    lat: float,
    lon: float,
    visit_min: int | None = None,
    best_months: tuple[int, ...] | None = None,
    rating: int | None = None,
) -> Poi:
    return Poi(
        name=name,
        lat=lat,
        lon=lon,
        tags=(),
        summary=None,
        visit_min=visit_min,
        best_months=best_months,
        rating=rating,
    )


Calls = list[tuple[tuple[float, float], tuple[float, float]]]


def fixed_drive(minutes: dict[tuple[float, float], float]) -> tuple[DriveTime, Calls]:
    """A drive-time edge that answers by destination, recording each call."""
    calls: Calls = []

    def drive_s(origin: tuple[float, float], dest: tuple[float, float]) -> float:
        calls.append((origin, dest))
        for (lat, lon), m in minutes.items():
            if abs(lat - dest[0]) < 1e-6 and abs(lon - dest[1]) < 1e-6:
                return m * 60.0
        raise AssertionError(f"unexpected lookup {dest}")

    return drive_s, calls


def test_reach_from_json_defaults_to_three_hour_round_trip_detour() -> None:
    assert reach_from_json(None) == Reach("detour", 3.0)
    assert reach_from_json({"mode": "each_way", "hours": 1.5}) == Reach("each_way", 1.5)


@pytest.mark.parametrize(
    "bad", [{"mode": "teleport", "hours": 1}, {"mode": "detour", "hours": 0}, {"hours": "x"}]
)
def test_reach_from_json_rejects_nonsense(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        reach_from_json(bad)


def test_detour_mode_uses_round_trip_drive_time_plus_reports_visit() -> None:
    near = poi("Near", 41.0, -110.3, visit_min=60, best_months=(5, 6), rating=4)  # ~16 mi off
    drive, _ = fixed_drive({(41.0, -110.3): 25.0})
    hits = near_route(ROUTE, DEPART, [near], Reach("detour", 3.0), drive)
    assert len(hits) == 1
    h = hits[0]
    assert h.poi is near
    assert h.status == "ok"
    assert h.one_way_min == pytest.approx(25.0)
    assert h.detour_min == pytest.approx(50.0)  # out and back to the join point
    assert h.mile == pytest.approx(75.0, abs=1.0)  # halfway along
    assert h.eta == datetime(2027, 5, 10, 18, 30, tzinfo=UTC)  # 90 min in
    assert h.in_season is True
    assert h.off_route_mi == pytest.approx(15.7, abs=0.5)


def test_detour_mode_drops_poi_whose_exact_detour_is_too_long() -> None:
    p = poi("Far-ish", 41.0, -110.5)  # ~26 mi off: passes the crow-flies prefilter
    drive, _ = fixed_drive({(41.0, -110.5): 100.0})  # but 200 min round trip by road
    assert near_route(ROUTE, DEPART, [p], Reach("detour", 3.0), drive) == []


def test_prefilter_skips_lookups_for_pois_that_cannot_be_reached() -> None:
    # 3 degrees west is ~157 crow miles: even at 70 mph that's > 90 min each way,
    # so a 3 h round trip is impossible and Google is never asked.
    far = poi("Far", 41.0, -113.0)
    drive, calls = fixed_drive({})
    assert near_route(ROUTE, DEPART, [far], Reach("detour", 3.0), drive) == []
    assert calls == []


def test_each_way_mode_allows_the_full_budget_one_way() -> None:
    p = poi("Two hours out", 41.0, -112.0)  # ~104 mi off
    drive, _ = fixed_drive({(41.0, -112.0): 150.0})
    assert near_route(ROUTE, DEPART, [p], Reach("detour", 3.0), drive) == []
    hits = near_route(ROUTE, DEPART, [p], Reach("each_way", 3.0), drive)
    assert [h.poi.name for h in hits] == ["Two hours out"]


def test_ahead_mode_keeps_only_pois_joined_within_the_window() -> None:
    early = poi("Early", 40.3, -110.1)  # joins ~22 road mi in, ~27 min
    late = poi("Late", 41.8, -110.1)  # joins ~135 road mi in, ~2.7 h
    drive, calls = fixed_drive({(40.3, -110.1): 10.0, (41.8, -110.1): 10.0})
    hits = near_route(ROUTE, DEPART, [early, late], Reach("ahead", 1.0), drive)
    assert [h.poi.name for h in hits] == ["Early"]
    assert len(calls) == 1  # "Late" was outside the window, no lookup


def test_ahead_mode_caps_how_far_off_the_route() -> None:
    p = poi("Off", 40.3, -111.0)
    drive, _ = fixed_drive({(40.3, -111.0): AHEAD_MAX_ONE_WAY_MIN + 5})
    assert near_route(ROUTE, DEPART, [p], Reach("ahead", 3.0), drive) == []


def test_failed_lookup_is_reported_without_a_number() -> None:
    p = poi("Flaky", 41.0, -110.3)

    def drive(o: tuple[float, float], d: tuple[float, float]) -> float:
        raise OSError("no internet")

    hits = near_route(ROUTE, DEPART, [p], Reach("detour", 3.0), drive)
    assert len(hits) == 1
    assert hits[0].status == "lookup_failed"
    assert hits[0].one_way_min is None
    assert hits[0].detour_min is None


def test_lookups_are_capped_nearest_first_and_the_rest_marked_unchecked() -> None:
    pois = [poi(f"P{i:02d}", 41.0, -110.05 - i * 0.01) for i in range(MAX_LOOKUPS + 3)]
    drive, calls = fixed_drive({(p.lat, p.lon): 10.0 for p in pois})
    hits = near_route(ROUTE, DEPART, pois, Reach("detour", 3.0), drive)
    assert len(calls) == MAX_LOOKUPS
    unchecked = [h.poi.name for h in hits if h.status == "unchecked"]
    assert unchecked == [f"P{i:02d}" for i in range(MAX_LOOKUPS, MAX_LOOKUPS + 3)]
    assert all(h.detour_min is None for h in hits if h.status == "unchecked")


def test_cached_lookups_are_reused() -> None:
    p = poi("Near", 41.0, -110.3)
    drive, calls = fixed_drive({(41.0, -110.3): 25.0})
    cache: dict[tuple[float, float, float, float], float] = {}
    near_route(ROUTE, DEPART, [p], Reach("detour", 3.0), drive, cache)
    near_route(ROUTE, DEPART, [p], Reach("each_way", 2.0), drive, cache)
    assert len(calls) == 1


def test_season_fit_is_unknown_without_best_months_and_false_out_of_season() -> None:
    a = poi("No season", 41.0, -110.2)
    b = poi("Winter only", 41.0, -110.25, best_months=(12, 1, 2))
    drive, _ = fixed_drive({(41.0, -110.2): 10.0, (41.0, -110.25): 12.0})
    hits = {h.poi.name: h for h in near_route(ROUTE, DEPART, [a, b], Reach("detour", 3.0), drive)}
    assert hits["No season"].in_season is None
    assert hits["Winter only"].in_season is False


def test_order_is_detour_then_rating_then_season_with_unknowns_last() -> None:
    quick = poi("Quick", 41.0, -110.2)
    rated = poi("Rated", 40.5, -110.4, rating=5)
    rated_low = poi("Rated low", 41.5, -110.4, rating=2)
    flaky = poi("Flaky", 41.2, -110.1)

    def drive(o: tuple[float, float], d: tuple[float, float]) -> float:
        table = {(41.0, -110.2): 10.0, (40.5, -110.4): 30.0, (41.5, -110.4): 30.0}
        if d in table:
            return table[d] * 60
        raise OSError("timeout")

    hits = near_route(ROUTE, DEPART, [flaky, rated_low, rated, quick], Reach("detour", 3.0), drive)
    assert [h.poi.name for h in hits] == ["Quick", "Rated", "Rated low", "Flaky"]
