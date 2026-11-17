"""The synthetic drive fixture must be generated, and must agree with the
hand-derived expected.json — independently of any bigair logic."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.support import DRIVE_DIR, Drive, parse_ts


def _load_generator() -> object:
    spec = importlib.util.spec_from_file_location("generate", DRIVE_DIR / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need their module registered
    spec.loader.exec_module(module)
    return module


def test_fixture_is_generated() -> None:
    """drive.csv must equal a fresh render — edit generate.py, not the CSV."""
    generator = _load_generator()
    fresh = generator.render()  # type: ignore[attr-defined]
    assert Path(DRIVE_DIR / "drive.csv").read_text() == fresh


def _sum_between(drive: Drive, start: str, end: str) -> tuple[float, float]:
    """Left-Riemann miles and gallons over [start, end) at 1 Hz."""
    t0, t1 = parse_ts(start), parse_ts(end)
    miles = gallons = 0.0
    for s in drive.obd:
        if t0 <= s.ts < t1 and s.ignition:
            miles += (s.speed_mph or 0.0) / 3600.0
            gallons += (s.fuel_rate_gph or 0.0) / 3600.0
    return miles, gallons


def test_pre_fill_totals_match_expected(drive: Drive) -> None:
    exp = drive.expected
    miles, gallons = _sum_between(drive, exp["times"]["start"], exp["pre_fill"]["at"])
    assert miles == pytest.approx(exp["pre_fill"]["miles"], abs=0.05)
    assert gallons == pytest.approx(exp["pre_fill"]["engine_used_gal"], abs=0.01)
    assert exp["capacity_gal"] - gallons == pytest.approx(exp["pre_fill"]["tank_gal"], abs=0.01)
    assert miles / gallons == pytest.approx(exp["pre_fill"]["mpg_rolling"], abs=0.01)


def test_post_fill_totals_match_expected(drive: Drive) -> None:
    exp = drive.expected
    miles, gallons = _sum_between(drive, exp["times"]["ignition_on"], exp["end"]["at"])
    assert miles == pytest.approx(exp["end"]["miles_since_fill"], abs=0.05)
    assert gallons == pytest.approx(exp["end"]["engine_used_since_fill_gal"], abs=0.01)
    window_mpg = (miles + exp["pre_fill"]["miles"]) / (gallons + exp["pre_fill"]["engine_used_gal"])
    assert window_mpg == pytest.approx(exp["end"]["mpg_rolling"], abs=0.01)
    assert exp["end"]["tank_gal"] * window_mpg == pytest.approx(exp["end"]["range_mi"], abs=1.0)


def test_level_jumps_across_the_park(drive: Drive) -> None:
    exp = drive.expected
    park, on = parse_ts(exp["times"]["park_start"]), parse_ts(exp["times"]["ignition_on"])
    before = [s.fuel_level_pct for s in drive.obd if s.ts < park and s.ignition][-1]
    after = next(s.fuel_level_pct for s in drive.obd if s.ts >= on)
    assert before == exp["fill_up"]["level_before_pct"]
    assert after == exp["fill_up"]["level_after_pct"]
    jump_gal = (after - before) / 100.0 * exp["capacity_gal"]  # type: ignore[operator]
    assert jump_gal == pytest.approx(exp["fill_up"]["gallons"], abs=0.01)


def test_ecu_is_silent_while_parked(drive: Drive) -> None:
    parked = [s for s in drive.obd if not s.ignition]
    assert len(parked) == 1200
    assert all(s.speed_mph is None and s.fuel_level_pct is None for s in parked)
