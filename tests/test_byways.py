"""Scenic byways along a route (#100). Synthetic lines only."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bigair.byways import ByWayIndex, choose_route, load_byways
from bigair.fuel_sources import RouteInfo

if TYPE_CHECKING:
    from pathlib import Path

# A byway running due north along lon -111.0 from lat 40.0 to 40.5 (~34.5 mi).
BYWAY = [[-111.0, 40.0], [-111.0, 40.25], [-111.0, 40.5]]


def _file(tmp_path: Path, features: list[dict[str, object]]) -> Path:
    f = tmp_path / "byways.geojson"
    f.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return f


def _line(name: str, coords: list[list[float]], multi: bool = False) -> dict[str, object]:
    geom = (
        {"type": "MultiLineString", "coordinates": [coords]}
        if multi
        else {"type": "LineString", "coordinates": coords}
    )
    return {"type": "Feature", "properties": {"Trail_Name": name}, "geometry": geom}


def test_load_reads_lines_and_multilines(tmp_path: Path) -> None:
    f = _file(tmp_path, [_line("North Rim Road", BYWAY), _line("Loop", BYWAY, multi=True),
                         {"type": "Feature", "properties": {}, "geometry": None}])  # fmt: skip
    idx, warnings = load_byways(f)
    assert idx is not None
    assert sorted(idx.names) == ["Loop", "North Rim Road"]
    assert warnings == ["byway feature 3 skipped (no line)"]


def test_missing_file_is_no_index(tmp_path: Path) -> None:
    assert load_byways(tmp_path / "absent.geojson") == (None, [])


def test_byway_miles_counts_route_stretches_on_the_byway(tmp_path: Path) -> None:
    idx, _ = load_byways(_file(tmp_path, [_line("North Rim Road", BYWAY)]))
    assert idx is not None
    # Route: lat 39.5 → 41.0 along the byway's longitude, 1.5° ≈ 103.6 mi.
    on = RouteInfo.from_points([(39.5 + i * 0.05, -111.0) for i in range(31)], 103.6, 7200)
    miles, names = idx.byway_miles(on)
    assert miles == pytest.approx(34.5, abs=3.5)  # the 0.5° that overlaps
    assert names == {"North Rim Road": pytest.approx(34.5, abs=3.5)}
    # A parallel route 10 mi east never touches it.
    off = RouteInfo.from_points([(39.5 + i * 0.05, -110.81) for i in range(31)], 103.6, 7200)
    assert idx.byway_miles(off) == (0.0, {})


def _route(lon: float, road_mi: float, hours: float) -> RouteInfo:
    return RouteInfo.from_points([(39.5 + i * 0.05, lon) for i in range(31)], road_mi, hours * 3600)


def test_choose_route_prefers_byway_miles_within_the_rules(tmp_path: Path) -> None:
    idx, _ = load_byways(_file(tmp_path, [_line("North Rim Road", BYWAY)]))
    assert idx is not None
    fast = _route(-110.81, 100.0, 2.0)  # no byway
    scenic = _route(-111.0, 115.0, 2.5)  # on the byway
    best, why = choose_route([fast, scenic], idx, window_min=300, max_mi=300)
    assert best is scenic
    assert why.startswith("most scenic of 2")


def test_choose_route_never_breaks_the_day_for_a_byway(tmp_path: Path) -> None:
    idx, _ = load_byways(_file(tmp_path, [_line("North Rim Road", BYWAY)]))
    assert idx is not None
    fast = _route(-110.81, 100.0, 2.0)
    scenic_but_long = _route(-111.0, 320.0, 6.0)  # over 300 mi and the window
    best, _ = choose_route([fast, scenic_but_long], idx, window_min=300, max_mi=300)
    assert best is fast


def test_choose_route_ties_go_to_the_shorter(tmp_path: Path) -> None:
    idx, _ = load_byways(_file(tmp_path, [_line("North Rim Road", BYWAY)]))
    assert idx is not None
    a = _route(-110.81, 120.0, 2.5)
    b = _route(-110.81, 100.0, 2.0)
    best, why = choose_route([a, b], idx, window_min=300, max_mi=300)
    assert best is b
    assert "no byways" in why


def test_choose_route_with_nothing_fitting_keeps_googles_first() -> None:
    a = _route(-110.81, 400.0, 8.0)
    b = _route(-110.81, 410.0, 8.5)
    best, why = choose_route([a, b], ByWayIndex.empty(), window_min=300, max_mi=300)
    assert best is a
    assert "fit the day" in why and why.startswith("none of 2")
