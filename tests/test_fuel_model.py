"""Fuel integrator and tank level (#7). Rules: .claude/skills/fuel-model/SKILL.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.fuel_model import FuelModel, FuelModelConfig
from bigair.signals import FillType, FuelState, ObdSample
from tests.support import Drive, parse_ts

T0 = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)


def _model(capacity: float = 47.0, prior_mpg: float = 15.0) -> FuelModel:
    state = FuelState(
        ts=T0,
        gal_after_last_fill=capacity,
        engine_used_gal=0.0,
        heater_used_gal=0.0,
        last_fill_type=FillType.FULL,
    )
    return FuelModel(FuelModelConfig(capacity_gal=capacity, prior_mpg=prior_mpg), state)


def _s(sec: float, gph: float | None, mph: float | None = 0.0, ignition: bool = True) -> ObdSample:
    return ObdSample(
        ts=T0 + timedelta(seconds=sec), ignition=ignition, speed_mph=mph, fuel_rate_gph=gph
    )


def _feed(model: FuelModel, samples: list[ObdSample]) -> None:
    for s in samples:
        model.update(s)


def test_constant_rate_for_an_hour_burns_that_many_gallons() -> None:
    m = _model()
    _feed(m, [_s(i, 3.6) for i in range(3601)])
    assert m.state.engine_used_gal == pytest.approx(3.6, abs=1e-9)
    assert m.tank_gal == pytest.approx(47.0 - 3.6, abs=1e-9)


def test_integration_is_trapezoidal() -> None:
    m = _model()
    _feed(m, [_s(0, 0.0), _s(1, 7.2)])
    assert m.state.engine_used_gal == pytest.approx(3.6 / 3600, rel=1e-9)


def test_first_sample_only_sets_the_baseline() -> None:
    m = _model()
    m.update(_s(0, 10.0))
    assert m.state.engine_used_gal == 0.0


def test_ignition_off_burns_no_engine_fuel() -> None:
    m = _model()
    _feed(m, [_s(0, 3.6, ignition=False), _s(1, 3.6, ignition=False), _s(2, 3.6)])
    assert m.state.engine_used_gal == 0.0


def test_gap_with_ignition_on_is_estimated_from_distance_not_bridged() -> None:
    """60 s gap at 60 mph = 1 mile; at the 15 mpg prior that's 1/15 gal, tracked as gap_gal."""
    m = _model(prior_mpg=15.0)
    _feed(m, [_s(0, 30.0, mph=60.0), _s(60, 30.0, mph=60.0)])
    assert m.state.gap_gal == pytest.approx(1 / 15, rel=1e-9)
    assert m.state.engine_used_gal == pytest.approx(1 / 15, rel=1e-9)  # not 30 gph × 60 s


def test_gap_while_stationary_adds_nothing() -> None:
    m = _model()
    _feed(m, [_s(0, 0.6, mph=0.0), _s(30, 0.6, mph=0.0)])
    assert m.state.engine_used_gal == 0.0


@pytest.mark.parametrize("bad", [-1.0, 40.1, 1000.0])
def test_absurd_rates_are_dropped(bad: float) -> None:
    m = _model()
    _feed(m, [_s(0, 3.6), _s(1, bad), _s(2, 3.6)])
    # The bad sample is skipped; 0 → 2 s is still within max_gap, so it's integrated.
    assert m.state.engine_used_gal == pytest.approx(3.6 * 2 / 3600, rel=1e-9)


def test_duplicate_and_out_of_order_samples_are_ignored() -> None:
    m = _model()
    _feed(m, [_s(0, 3.6), _s(1, 3.6), _s(1, 3.6), _s(0.5, 3.6), _s(2, 3.6)])
    assert m.state.engine_used_gal == pytest.approx(3.6 * 2 / 3600, rel=1e-9)


def test_tank_subtracts_engine_and_heater_from_level_after_fill() -> None:
    state = FuelState(
        ts=T0,
        gal_after_last_fill=40.0,
        engine_used_gal=5.0,
        heater_used_gal=1.5,
        last_fill_type=FillType.PARTIAL,
    )
    assert FuelModel(FuelModelConfig(), state).tank_gal == pytest.approx(33.5)


def test_no_valid_rate_for_a_minute_with_ignition_on_is_stale() -> None:
    m = _model()
    _feed(m, [_s(0, 3.6)] + [_s(i, None) for i in range(1, 62)])
    assert m.state.stale


def test_a_valid_rate_clears_stale() -> None:
    m = _model()
    _feed(m, [_s(0, 3.6)] + [_s(i, None) for i in range(1, 62)] + [_s(62, 3.6)])
    assert not m.state.stale


def test_ignition_off_is_never_stale() -> None:
    m = _model()
    _feed(m, [_s(0, 3.6)] + [_s(i, None, ignition=False) for i in range(1, 200)])
    assert not m.state.stale


def test_tank_before_fill_matches_the_synthetic_drive(drive: Drive) -> None:
    exp = drive.expected
    m = _model(capacity=exp["capacity_gal"], prior_mpg=exp["initial"]["prior_mpg"])
    for sample in drive.until(parse_ts(exp["pre_fill"]["at"])):
        m.update(sample)
    assert m.state.engine_used_gal == pytest.approx(exp["pre_fill"]["engine_used_gal"], abs=0.01)
    assert m.tank_gal == pytest.approx(exp["pre_fill"]["tank_gal"], abs=0.01)
    assert m.state.gap_gal == 0.0
    assert not m.state.stale


# --- #8: rolling mpg, range, instant mpg -------------------------------------


def _cruise(start_s: int, seconds: int, mph: float, gph: float) -> list[ObdSample]:
    return [_s(start_s + i, gph, mph=mph) for i in range(seconds + 1)]


def _drive(m: FuelModel, miles: float, mpg: float, start_s: int = 0, mph: float = 60.0) -> int:
    """Cruise `miles` at `mpg`; return the time of the last sample."""
    seconds = round(miles / mph * 3600)
    _feed(m, _cruise(start_s, seconds, mph, mph / mpg))
    return start_s + seconds


def test_rolling_mpg_uses_the_prior_below_the_minimum_history() -> None:
    m = _model(prior_mpg=17.0)
    _drive(m, 40.0, 10.0)  # 40 mi < 50 mi minimum
    assert m.mpg_rolling == pytest.approx(17.0)


def test_rolling_mpg_with_no_history_is_the_prior() -> None:
    assert _model(prior_mpg=17.0).mpg_rolling == pytest.approx(17.0)


def test_rolling_mpg_is_distance_over_fuel_once_history_is_enough() -> None:
    m = _model(prior_mpg=17.0)
    _drive(m, 60.0, 12.0)
    assert m.mpg_rolling == pytest.approx(12.0, rel=1e-3)


def test_rolling_window_keeps_only_the_most_recent_150_miles() -> None:
    m = _model()
    t = _drive(m, 150.0, 10.0)
    _drive(m, 150.0, 20.0, start_s=t + 1)
    # Window granularity is 1 mi, so at most ~1 mi of the old 10 mpg leg remains.
    assert m.mpg_rolling == pytest.approx(20.0, rel=0.01)


def test_idle_fuel_counts_against_rolling_mpg() -> None:
    """Idling burns fuel without miles; it belongs in the rolling window."""
    m = _model()
    t = _drive(m, 60.0, 12.0)  # 5 gal
    _feed(m, _cruise(t + 1, 3600, 0.0, 1.0))  # 1 gal idling, 0 mi
    assert m.mpg_rolling == pytest.approx(60.0 / 6.0, rel=1e-3)


def test_gap_estimates_are_excluded_from_rolling_mpg() -> None:
    m = _model(prior_mpg=15.0)
    t = _drive(m, 60.0, 12.0)
    _feed(m, [_s(t + 600, 5.0, mph=60.0)])  # 10-minute gap: 10 mi estimated
    assert m.state.gap_gal > 0
    assert m.mpg_rolling == pytest.approx(12.0, rel=1e-3)


def test_gaps_are_estimated_with_rolling_mpg_once_available() -> None:
    m = _model(prior_mpg=15.0)
    t = _drive(m, 60.0, 12.0)
    _feed(m, [_s(t + 60, 5.0, mph=60.0)])  # 60 s at 60 mph = 1 mi
    assert m.state.gap_gal == pytest.approx(1 / 12.0, rel=1e-3)


def test_range_is_tank_times_rolling_mpg() -> None:
    m = _model(prior_mpg=15.0)
    assert m.range_mi == pytest.approx(47.0 * 15.0)


def test_range_never_goes_negative() -> None:
    state = FuelState(
        ts=T0,
        gal_after_last_fill=5.0,
        engine_used_gal=6.0,
        heater_used_gal=0.0,
        last_fill_type=FillType.PARTIAL,
    )
    assert FuelModel(FuelModelConfig(), state).range_mi == 0.0


def test_instant_mpg_on_a_steady_cruise() -> None:
    m = _model()
    _feed(m, _cruise(0, 120, 60.0, 4.0))
    assert m.mpg_instant_30s == pytest.approx(15.0, rel=1e-9)


def test_instant_mpg_only_looks_at_the_last_30_seconds() -> None:
    m = _model()
    _feed(m, _cruise(0, 120, 60.0, 6.0))  # 10 mpg
    _feed(m, _cruise(121, 60, 60.0, 3.0))  # 20 mpg for the last minute
    assert m.mpg_instant_30s == pytest.approx(20.0, rel=1e-9)


def test_instant_mpg_is_none_when_barely_moving() -> None:
    m = _model()
    _feed(m, _cruise(0, 60, 5.0, 0.6))  # 30 s at 5 mph = 0.042 mi < 0.05
    assert m.mpg_instant_30s is None


def test_instant_mpg_is_none_with_no_samples() -> None:
    assert _model().mpg_instant_30s is None


def test_instant_mpg_is_capped_on_overrun_fuel_cut() -> None:
    m = _model()
    _feed(m, _cruise(0, 60, 50.0, 0.0))  # coasting downhill, injectors off
    assert m.mpg_instant_30s == pytest.approx(50.0)


def test_instant_mpg_window_follows_the_clock_after_ignition_off() -> None:
    m = _model()
    _feed(m, _cruise(0, 60, 60.0, 4.0))
    _feed(m, [_s(61 + i, None, mph=0.0, ignition=False) for i in range(40)])
    assert m.mpg_instant_30s is None
