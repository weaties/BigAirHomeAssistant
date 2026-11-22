"""Gauge-feed fields behind the Trip and Van dash screens (#62)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.service import FuelService, ServiceConfig
from bigair.signals import GpsFix, ObdSample

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)


def _svc(tmp_path: Path) -> FuelService:
    return FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))


def test_feed_without_any_input(tmp_path: Path) -> None:
    feed = _svc(tmp_path).gauge_feed(T0)
    assert feed["trip_mi"] == 0.0
    assert feed["trip_gal"] == 0.0
    assert feed["trip_start"] is None
    assert feed["obd_ts"] is None
    assert feed["ignition"] is None
    assert feed["gps_ts"] is None
    assert feed["gps_hdop"] is None
    assert feed["gps_lat"] is None and feed["gps_lon"] is None


def test_feed_carries_trip_obd_and_gps(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(361):  # 6 mi at 15 mpg
        svc.handle_sample(
            ObdSample(
                ts=T0 + timedelta(seconds=i), ignition=True, speed_mph=60.0, fuel_rate_gph=4.0
            )
        )
    svc.handle_fix(GpsFix(ts=T0 + timedelta(seconds=359), lat=45.123456, lon=-110.987654, hdop=0.9))
    feed = svc.gauge_feed(T0 + timedelta(seconds=361))
    assert feed["trip_mi"] == pytest.approx(6.0, abs=0.01)
    assert feed["trip_gal"] == pytest.approx(0.4, abs=0.001)
    assert feed["trip_start"] == "2026-11-21T18:00:00Z"
    assert feed["obd_ts"] == "2026-11-21T18:06:00Z"
    assert feed["ignition"] is True
    assert feed["gps_ts"] == "2026-11-21T18:05:59Z"
    assert feed["gps_hdop"] == 0.9
    assert feed["gps_lat"] == 45.123456 and feed["gps_lon"] == -110.987654
