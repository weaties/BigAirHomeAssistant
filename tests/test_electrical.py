"""House electrical tracker (#109): latest reading per device → MQTT state + Influx."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from bigair.electrical import INFLUX_EVERY_S, STALE_AFTER_S, ElectricalTracker
from bigair.influx_io import to_line
from bigair.signals import VictronSample

T0 = datetime(2026, 11, 26, 23, 40, tzinfo=UTC)


def _bmv(sec: float, **kw: object) -> VictronSample:
    base: dict[str, object] = {
        "voltage_v": 13.27,
        "current_a": -3.663,
        "power_w": -48.608,
        "soc_pct": 99.2,
        "consumed_ah": -1.7,
        "time_to_go_s": 207360,
        "alarm": 0,
    }
    base.update(kw)
    return VictronSample(ts=T0 + timedelta(seconds=sec), mono_s=100 + sec, device="bmv", **base)  # type: ignore[arg-type]


def _mppt(sec: float) -> VictronSample:
    return VictronSample(
        ts=T0 + timedelta(seconds=sec),
        mono_s=100 + sec,
        device="mppt",
        state="bulk",
        charger_error=0,
        voltage_v=13.28,
        current_a=0.5,
        yield_today_wh=60.0,
        pv_power_w=9,
    )


def _dcdc(sec: float) -> VictronSample:
    return VictronSample(
        ts=T0 + timedelta(seconds=sec),
        mono_s=100 + sec,
        device="dcdc",
        state="off",
        charger_error=0,
        input_voltage_v=12.72,
        off_reason=1,
    )


def test_first_reading_per_device_is_written_then_throttled() -> None:
    t = ElectricalTracker()
    assert t.update(_bmv(0))
    assert t.update(_bmv(1)) == []
    assert t.update(_mppt(1))  # throttled per device
    assert t.update(_bmv(INFLUX_EVERY_S))


def test_influx_point_is_tagged_by_device_with_unit_suffixed_fields() -> None:
    (p,) = ElectricalTracker().update(_bmv(0))
    assert to_line(p) == (
        "electrical,device=bmv voltage_v=13.27,current_a=-3.663,power_w=-48.608,soc_pct=99.2,"
        "consumed_ah=-1.7,time_to_go_s=207360i,alarm=0i 1795736400000"
    )


def test_solar_and_dcdc_points_carry_their_own_fields() -> None:
    t = ElectricalTracker()
    (solar,) = t.update(_mppt(0))
    (dcdc,) = t.update(_dcdc(0))
    assert solar.fields["pv_power_w"] == 9
    assert solar.fields["yield_today_wh"] == 60.0
    assert solar.fields["state"] == "bulk"
    assert dcdc.fields["input_voltage_v"] == 12.72
    assert dcdc.fields["off_reason"] == 1
    assert "voltage_v" not in dcdc.fields  # None fields are left out


def test_state_payload_has_every_key_and_nulls_until_seen() -> None:
    t = ElectricalTracker()
    payload = json.loads(t.state_payload(mono_now=100))
    assert set(payload) == set(t.KEYS)
    assert all(v is None for v in payload.values())  # no meter: energy keys null too


def test_state_payload_maps_devices_to_house_solar_and_dcdc() -> None:
    t = ElectricalTracker()
    for s in (_bmv(0), _mppt(0), _dcdc(0)):
        t.update(s)
    p = json.loads(t.state_payload(mono_now=101))
    assert p["house_soc_pct"] == pytest.approx(99.2)
    assert p["house_voltage_v"] == pytest.approx(13.27)
    assert p["house_current_a"] == pytest.approx(-3.663)
    assert p["house_power_w"] == pytest.approx(-48.6, abs=0.05)
    assert p["house_consumed_ah"] == pytest.approx(-1.7)
    assert p["house_time_to_go_min"] == 3456
    assert p["solar_power_w"] == 9
    assert p["solar_yield_today_wh"] == 60.0
    assert p["solar_state"] == "bulk"
    assert p["dcdc_state"] == "off"
    assert p["dcdc_input_voltage_v"] == pytest.approx(12.72)


def test_a_device_gone_quiet_goes_unknown() -> None:
    """A stale battery number is worse than none (CLAUDE.md)."""
    t = ElectricalTracker()
    t.update(_bmv(0))
    t.update(_mppt(STALE_AFTER_S))
    p = json.loads(t.state_payload(mono_now=100 + STALE_AFTER_S + 1))
    assert p["house_soc_pct"] is None
    assert p["solar_power_w"] == 9


def test_freshness_uses_the_monotonic_clock() -> None:
    """A wall-clock step (#107) mustn't make a fresh reading look stale."""
    t = ElectricalTracker()
    s = _bmv(0)
    t.update(VictronSample(**{**s.__dict__, "ts": s.ts - timedelta(days=6)}))
    assert json.loads(t.state_payload(mono_now=101))["house_soc_pct"] == pytest.approx(99.2)


def test_victron_keys_from_env_pairs() -> None:
    from bigair.electrical import victron_keys

    env = {
        "VICTRON_BMV_MAC": "f1:db:c7:00:00:01",
        "VICTRON_BMV_KEY": "00" * 16,
        "VICTRON_SOLAR_MAC": "D9:FC:22:00:00:02",
        "VICTRON_SOLAR_KEY": "zz",
        "VICTRON_ORION_MAC": "not-a-mac",
        "VICTRON_ORION_KEY": "11" * 16,
        "OTHER": "x",
    }
    keys, problems = victron_keys(env)
    assert keys == {"F1:DB:C7:00:00:01": bytes(16)}
    assert problems == [
        "VICTRON_ORION_MAC isn't a MAC address",
        "VICTRON_SOLAR_KEY isn't 32 hex digits",
    ]


def test_with_a_meter_bmv_points_bring_house_flows_and_daily_energy() -> None:
    from zoneinfo import ZoneInfo

    from bigair.energy import EnergyMeter

    t = ElectricalTracker(EnergyMeter(ZoneInfo("America/Los_Angeles")))
    t.update(_mppt(0))
    t.update(_dcdc(0))
    points = t.update(_bmv(0))
    kinds = [(p.measurement, p.tags.get("device")) for p in points]
    assert kinds == [("electrical", "bmv"), ("electrical", "house"), ("energy", None)]
    house = points[1].fields
    # solar 13.28 V × 0.5 A = 6.64 W, net −48.608 W → loads 55.2 W; Orion off → 0 W
    assert house == {"loads_w": 55.2, "alternator_w": 0.0, "estimated": False}
    p = json.loads(t.state_payload(mono_now=101))
    assert p["loads_w"] == pytest.approx(55.2)
    assert p["alternator_w"] == 0.0
    assert p["energy_estimated"] is False
    assert p["loads_today_wh"] == 0.0  # a single reading integrates nothing
    assert json.loads(t.state_payload(mono_now=100 + STALE_AFTER_S + 1))["loads_w"] is None
