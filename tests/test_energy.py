"""Daily energy by source and load (#113). Hand-derived Wh from constant powers.

Flows: net = BMV power (+ charging). Solar = MPPT battery V × A. With the alternator
charger off, loads = solar − net. While the Orion charges (its record has no current),
loads are held at their last value and alternator = net − solar + loads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bigair.energy import MAX_GAP_S, EnergyMeter
from bigair.signals import VictronSample

PT = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 10:00 PST


def _bmv(sec: float, watts: float, mono: float | None = None) -> VictronSample:
    return VictronSample(
        ts=T0 + timedelta(seconds=sec),
        mono_s=sec if mono is None else mono,
        device="bmv",
        voltage_v=13.0,
        current_a=watts / 13.0,
        power_w=watts,
    )


def _solar(sec: float, watts: float) -> VictronSample:
    return VictronSample(
        ts=T0 + timedelta(seconds=sec),
        mono_s=sec,
        device="mppt",
        voltage_v=13.0,
        current_a=watts / 13.0,
        pv_power_w=int(watts) + 5,
        state="bulk",
    )


def _orion(sec: float, state: str) -> VictronSample:
    return VictronSample(
        ts=T0 + timedelta(seconds=sec), mono_s=sec, device="dcdc", state=state, voltage_v=13.8
    )


def _hour(
    m: EnergyMeter,
    start: float,
    net_w: float,
    solar_w: float | None = None,
    orion: str | None = None,
) -> None:
    """One hour at constant power, sampled every 30 s (the BLE burst spacing)."""
    for i in range(121):
        t = start + i * 30
        if orion is not None:
            m.update(_orion(t, orion))
        if solar_w is not None:
            m.update(_solar(t, solar_w))
        m.update(_bmv(t, net_w))


def test_parked_with_loads_only() -> None:
    """−48 W for an hour, no sun: 48 Wh out of the battery, 48 Wh of loads."""
    m = EnergyMeter(PT)
    _hour(m, 0, -48.0, solar_w=0.0)
    t = m.today
    assert t.day == "2026-11-27"
    assert t.battery_out_wh == pytest.approx(48.0)
    assert t.battery_in_wh == 0.0
    assert t.loads_wh == pytest.approx(48.0)
    assert t.solar_wh == 0.0
    assert t.alternator_wh == 0.0
    assert m.flows.loads_w == pytest.approx(48.0)


def test_sun_covers_loads_and_charges() -> None:
    """200 W solar, battery +150 W: loads are 50 W. One hour: 200 / 150 in / 50 Wh."""
    m = EnergyMeter(PT)
    _hour(m, 0, 150.0, solar_w=200.0)
    t = m.today
    assert t.solar_wh == pytest.approx(200.0)
    assert t.battery_in_wh == pytest.approx(150.0)
    assert t.loads_wh == pytest.approx(50.0)
    assert t.estimated_wh == 0.0


def test_alternator_while_driving_holds_loads() -> None:
    """Parked at 40 W of loads, then the Orion charges: net +300 W, solar 20 W.
    Loads held at 40 W, so alternator = 300 − 20 + 40 = 320 W."""
    m = EnergyMeter(PT)
    _hour(m, 0, -40.0, solar_w=0.0, orion="off")
    _hour(m, 3630, 300.0, solar_w=20.0, orion="bulk")
    t = m.today
    assert m.flows.alternator_w == pytest.approx(320.0)
    assert m.flows.loads_w == pytest.approx(40.0)
    assert m.flows.estimated
    # Each hour, plus the 30 s transition between them (≤ 1.4 Wh):
    assert t.alternator_wh == pytest.approx(320.0, abs=1.5)
    assert t.loads_wh == pytest.approx(80.0, abs=1.5)  # 40 parked + 40 held
    assert t.estimated_wh == pytest.approx(40.0, abs=1.5)
    assert t.battery_in_wh == pytest.approx(300.0, abs=1.5)


def test_power_supply_mode_counts_as_charging() -> None:
    m = EnergyMeter(PT)
    m.update(_orion(0, "power_supply"))
    m.update(_solar(0, 0.0))
    m.update(_bmv(0, 100.0))
    assert m.flows.estimated


def test_gaps_longer_than_the_limit_are_not_integrated() -> None:
    """Pi off overnight: two readings hours apart add nothing (unlike Grafana's
    integral(), which would interpolate across the gap)."""
    m = EnergyMeter(PT)
    m.update(_solar(0, 0.0))
    m.update(_bmv(0, -50.0))
    m.update(_solar(MAX_GAP_S + 1, 0.0))
    m.update(_bmv(MAX_GAP_S + 1, -50.0))
    assert m.today.battery_out_wh == 0.0
    assert m.today.loads_wh == 0.0


def test_wall_clock_steps_dont_change_the_integral() -> None:
    """dt comes from monotonic time (#107)."""
    m = EnergyMeter(PT)
    m.update(_solar(0, 0.0))
    m.update(_bmv(0, -36.0, mono=100.0))
    s = _bmv(566_034, -36.0, mono=200.0)  # wall clock jumped 6.5 days
    m.update(VictronSample(**{**s.__dict__, "ts": T0 + timedelta(seconds=100)}))
    assert m.today.battery_out_wh == pytest.approx(1.0)


def test_unknown_solar_leaves_loads_unknown() -> None:
    """No MPPT reading: battery in/out still count, but loads aren't guessed."""
    m = EnergyMeter(PT)
    _hour(m, 0, -48.0, solar_w=None)
    assert m.today.battery_out_wh == pytest.approx(48.0)
    assert m.today.loads_wh == 0.0
    assert m.flows.loads_w is None


def test_day_rolls_over_at_local_midnight() -> None:
    m = EnergyMeter(PT)
    late = datetime(2026, 11, 28, 7, 59, tzinfo=UTC)  # 23:59 PST
    for i, sec in enumerate((0, 30, 60, 90)):
        ts = late + timedelta(seconds=sec)
        m.update(VictronSample(ts=ts, mono_s=sec, device="mppt", voltage_v=13, current_a=0))
        m.update(VictronSample(ts=ts, mono_s=sec, device="bmv", power_w=-36.0 - 0 * i))
    assert m.today.day == "2026-11-28"
    assert m.yesterday is not None and m.yesterday.day == "2026-11-27"
    assert m.yesterday.battery_out_wh + m.today.battery_out_wh == pytest.approx(36 * 90 / 3600)


def test_a_reading_dated_in_the_past_never_rolls_the_day_back() -> None:
    """Before chrony's GPS step the wall clock can be days behind."""
    m = EnergyMeter(PT)
    m.update(_bmv(0, -36.0))
    stale = VictronSample(ts=T0 - timedelta(days=6), mono_s=30, device="bmv", power_w=-36.0)
    m.update(stale)
    assert m.today.day == "2026-11-27"


def test_state_round_trips_within_the_same_day() -> None:
    m = EnergyMeter(PT)
    _hour(m, 0, -48.0, solar_w=0.0)
    saved = m.to_state()
    again = EnergyMeter(PT, saved)
    assert again.today == m.today


def test_influx_point_is_one_per_local_day() -> None:
    m = EnergyMeter(PT)
    _hour(m, 0, 150.0, solar_w=200.0)
    p = m.day_point()
    assert p.measurement == "energy"
    assert p.ts == datetime(2026, 11, 27, 8, 0, tzinfo=UTC)  # local midnight → same point
    assert p.fields["solar_wh"] == pytest.approx(200.0, abs=0.01)
    assert p.fields["loads_wh"] == pytest.approx(50.0, abs=0.01)
