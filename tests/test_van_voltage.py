"""Van battery voltage from the WiCAN's own ATRV reading → Influx, gauge feed, debug strip."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.display_render import DebugInfo, GaugeState, gauge_from_dict, with_van_voltage
from bigair.influx_io import vehicle_point
from bigair.obd_decode import decode_adapter_volts, sample_from_responses
from bigair.service import FuelService, ServiceConfig
from bigair.signals import ObdSample

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 20, 18, 40, tzinfo=UTC)


@pytest.mark.parametrize(("raw", "v"), [("14.2V", 14.2), ("12.6V\n", 12.6), (" 13.9 V ", 13.9)])
def test_atrv_decodes(raw: str, v: float) -> None:
    assert decode_adapter_volts(raw) == pytest.approx(v)


@pytest.mark.parametrize("raw", ["", "?", "NO DATA", "V", "99.0V"])
def test_atrv_rejects_garbage(raw: str) -> None:
    assert decode_adapter_volts(raw) is None


def test_battery_alone_does_not_mean_ignition_on() -> None:
    """The adapter answers ATRV even when the ECU is silent."""
    s = sample_from_responses(
        T0, {"0D": "NO DATA", "5E": "NO DATA", "2F": "NO DATA", "ATRV": "12.4V"}
    )
    assert not s.ignition
    assert s.battery_v == pytest.approx(12.4)


def test_vehicle_point_carries_battery_voltage() -> None:
    s = ObdSample(ts=T0, ignition=True, speed_mph=30.0, fuel_rate_gph=1.0, battery_v=14.1)
    assert vehicle_point(s).fields["battery_v"] == 14.1


def _svc(tmp_path: Path) -> FuelService:
    return FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))


def test_feed_has_fresh_van_voltage(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_sample(
        ObdSample(ts=T0, ignition=True, speed_mph=0.0, fuel_rate_gph=0.5, battery_v=14.1)
    )
    assert svc.gauge_feed(T0 + timedelta(seconds=10))["van_battery_v"] == 14.1
    assert svc.gauge_feed(T0 + timedelta(seconds=40))["van_battery_v"] is None  # WiCAN asleep


def test_display_parses_van_voltage() -> None:
    g = gauge_from_dict({"ts": "2026-11-20T18:40:00Z", "van_battery_v": 13.8})
    assert g.van_battery_v == 13.8


def test_debug_strip_falls_back_to_the_feed_voltage() -> None:
    debug = DebugInfo(updated=T0, pi_5v_v=5.0)
    gauge = GaugeState(tank_gal=None, range_mi=None, van_battery_v=13.8)
    assert with_van_voltage(debug, gauge).van_battery_v == 13.8
    host = DebugInfo(updated=T0, pi_5v_v=5.0, van_battery_v=12.9)
    assert with_van_voltage(host, gauge).van_battery_v == 12.9  # host value wins
    assert with_van_voltage(None, gauge) is None
