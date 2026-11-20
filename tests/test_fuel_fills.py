"""Fill reset, true mpg between fills, and model snapshots (#9). Rules: /fuel-model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.fuel_model import FuelModel, FuelModelConfig
from bigair.signals import FillSource, FillStatus, FillType, FillUp, FuelState, ObdSample

T0 = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
MPH = 60.0


def _model(capacity: float = 47.0) -> FuelModel:
    state = FuelState(
        ts=T0,
        gal_after_last_fill=capacity,
        engine_used_gal=0.0,
        heater_used_gal=0.0,
        last_fill_type=FillType.FULL,
    )
    return FuelModel(FuelModelConfig(capacity_gal=capacity, prior_mpg=15.0), state)


def _drive(m: FuelModel, start_s: int, miles: float, mpg: float) -> int:
    """Cruise at 60 mph; return the time of the last sample."""
    seconds = round(miles / MPH * 3600)
    for i in range(seconds + 1):
        m.update(
            ObdSample(
                ts=T0 + timedelta(seconds=start_s + i),
                ignition=True,
                speed_mph=MPH,
                fuel_rate_gph=MPH / mpg,
            )
        )
    return start_s + seconds


def _park(m: FuelModel, at_s: int) -> None:
    m.update(
        ObdSample(
            ts=T0 + timedelta(seconds=at_s), ignition=False, speed_mph=0.0, fuel_rate_gph=None
        )
    )


def _fill(
    at_s: int,
    gallons: float | None,
    kind: FillType = FillType.FULL,
    status: FillStatus = FillStatus.CONFIRMED,
    fill_id: str | None = None,
) -> FillUp:
    return FillUp(
        fill_id=fill_id or f"fill-{at_s}",
        ts=T0 + timedelta(seconds=at_s),
        source=FillSource.LIVE,
        fill_type=kind,
        status=status,
        gallons=gallons,
    )


def test_full_fill_sets_tank_to_capacity_and_resets_counters() -> None:
    m = _model()
    t = _drive(m, 0, 60.0, 12.0)  # 5 gal
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 5.0))
    assert m.tank_gal == pytest.approx(47.0)
    assert m.state.engine_used_gal == 0.0
    assert m.state.gap_gal == 0.0
    assert m.state.last_fill_type == FillType.FULL


def test_full_fill_gives_true_mpg_from_pump_gallons() -> None:
    m = _model()
    t = _drive(m, 0, 60.0, 12.0)
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 4.8))  # pump says 4.8, model said 5.0
    assert m.mpg_since_full == pytest.approx(60.0 / 4.8, rel=1e-3)


def test_partial_fill_adds_gallons_to_the_tank_before() -> None:
    m = _model()
    t = _drive(m, 0, 120.0, 12.0)  # 10 gal → tank 37
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 6.0, FillType.PARTIAL))
    assert m.tank_gal == pytest.approx(43.0, rel=1e-3)
    assert m.state.last_fill_type == FillType.PARTIAL
    assert m.mpg_since_full is None  # a partial gives no true mpg


def test_partial_fills_chain_into_the_next_full_fill_mpg() -> None:
    m = _model()
    t = _drive(m, 0, 120.0, 12.0)  # 10 gal
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 6.0, FillType.PARTIAL))
    t = _drive(m, t + 3, 60.0, 12.0)  # 5 gal → tank 38
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 9.0))
    # 180 mi since the previous full ÷ (6 + 9) pump gallons
    assert m.mpg_since_full == pytest.approx(180.0 / 15.0, rel=1e-3)
    assert m.tank_gal == pytest.approx(47.0)


def test_partial_fill_never_overfills_the_tank() -> None:
    m = _model()
    t = _drive(m, 0, 12.0, 12.0)  # 1 gal → tank 46
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 5.0, FillType.PARTIAL))
    assert m.tank_gal == pytest.approx(47.0)


def test_partial_fill_without_gallons_is_ignored() -> None:
    m = _model()
    t = _drive(m, 0, 12.0, 12.0)
    before = m.state
    m.apply_fill(_fill(t + 2, None, FillType.PARTIAL))
    assert m.state == before


def test_full_fill_without_gallons_resets_the_tank_but_gives_no_mpg() -> None:
    m = _model()
    t = _drive(m, 0, 60.0, 12.0)
    m.apply_fill(_fill(t + 2, None))
    assert m.tank_gal == pytest.approx(47.0)
    assert m.mpg_since_full is None


@pytest.mark.parametrize("status", [FillStatus.PENDING, FillStatus.SKIPPED])
def test_unconfirmed_fills_do_not_touch_the_tank(status: FillStatus) -> None:
    m = _model()
    t = _drive(m, 0, 60.0, 12.0)
    before = m.state
    m.apply_fill(_fill(t + 2, 5.0, status=status))
    assert m.state == before


def test_applying_the_same_fill_twice_is_a_no_op() -> None:
    m = _model()
    t = _drive(m, 0, 120.0, 12.0)
    fill = _fill(t + 2, 6.0, FillType.PARTIAL)
    m.apply_fill(fill)
    m.apply_fill(fill)
    assert m.tank_gal == pytest.approx(43.0, rel=1e-3)


def test_fill_confirmed_later_counts_fuel_burned_since_the_fill() -> None:
    """Dan answers the prompt 30 min down the road: fuel after the pump still counts."""
    m = _model()
    t = _drive(m, 0, 60.0, 12.0)  # 5 gal before the pump
    _park(m, t + 1)
    fill_at = t + 2
    _drive(m, t + 600, 30.0, 12.0)  # 2.5 gal after the pump, before confirmation
    m.apply_fill(_fill(fill_at, 5.0))
    assert m.tank_gal == pytest.approx(47.0 - 2.5, abs=0.01)
    assert m.mpg_since_full == pytest.approx(60.0 / 5.0, rel=1e-2)
    assert m.last_fill is not None
    assert m.last_fill.tank_before_gal == pytest.approx(42.0, abs=0.01)


def test_last_fill_reports_tank_before_and_after() -> None:
    m = _model()
    t = _drive(m, 0, 120.0, 12.0)
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 6.0, FillType.PARTIAL))
    assert m.last_fill is not None
    assert m.last_fill.tank_before_gal == pytest.approx(37.0, rel=1e-3)
    assert m.last_fill.tank_after_gal == pytest.approx(43.0, rel=1e-3)
    assert m.last_fill.mpg_since_full is None


def test_true_mpg_becomes_the_fresh_install_rolling_fallback() -> None:
    m = _model()
    t = _drive(m, 0, 40.0, 10.0)  # below the 50 mi minimum
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 4.0))  # 40 mi / 4 gal = 10 mpg
    assert m.mpg_rolling == pytest.approx(10.0, rel=1e-3)


def test_unknown_interval_start_gives_no_true_mpg() -> None:
    state = FuelState(
        ts=T0,
        gal_after_last_fill=30.0,
        engine_used_gal=0.0,
        heater_used_gal=0.0,
        last_fill_type=FillType.PARTIAL,
        miles_since_full=None,  # installed after a partial: no known full-fill start
    )
    m = FuelModel(FuelModelConfig(), state)
    t = _drive(m, 0, 60.0, 12.0)
    m.apply_fill(_fill(t + 2, 22.0))
    assert m.mpg_since_full is None
    assert m.tank_gal == pytest.approx(47.0)
    assert m.state.miles_since_full == pytest.approx(0.0)


def test_snapshot_round_trip_keeps_tank_and_rolling_window() -> None:
    m = _model()
    t = _drive(m, 0, 120.0, 12.0)
    _park(m, t + 1)
    m.apply_fill(_fill(t + 2, 6.0, FillType.PARTIAL))
    restored = FuelModel.from_snapshot(m.config, m.snapshot())
    assert restored.state == m.state
    assert restored.tank_gal == pytest.approx(m.tank_gal)
    assert restored.mpg_rolling == pytest.approx(m.mpg_rolling)
    assert restored.mpg_since_full == m.mpg_since_full


def test_restored_model_treats_the_first_sample_as_a_new_baseline() -> None:
    m = _model()
    t = _drive(m, 0, 60.0, 12.0)
    restored = FuelModel.from_snapshot(m.config, m.snapshot())
    used = restored.state.engine_used_gal
    restored.update(
        ObdSample(
            ts=T0 + timedelta(seconds=t + 1), ignition=True, speed_mph=60.0, fuel_rate_gph=5.0
        )
    )
    assert restored.state.engine_used_gal == used


def test_full_fill_with_no_miles_since_the_last_full_gives_no_mpg() -> None:
    m = _model()
    m.apply_fill(_fill(10, 3.0))
    assert m.mpg_since_full is None
