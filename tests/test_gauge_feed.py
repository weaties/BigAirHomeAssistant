"""Service → display gauge feed (#17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.display_render import GaugeState, gauge_from_dict, is_stale, render_gauge
from bigair.display_server import gauge_file_reader
from bigair.service import FuelService, ServiceConfig
from bigair.signals import ObdSample
from bigair.state_file import write_state

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


def _svc(tmp_path: Path) -> FuelService:
    return FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))


def test_feed_before_the_tank_is_known(tmp_path: Path) -> None:
    feed = _svc(tmp_path).gauge_feed(T0)
    assert feed["tank_gal"] is None and feed["range_mi"] is None
    assert feed["ts"] == "2026-11-20T18:00:00Z"
    assert feed["calibrating"] is True


def test_feed_carries_tank_range_and_trip_buckets(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    for i in range(1, 241):  # 4 mi at 15 mpg
        svc.handle_sample(
            ObdSample(
                ts=T0 + timedelta(seconds=i), ignition=True, speed_mph=60.0, fuel_rate_gph=4.0
            )
        )
    feed = svc.gauge_feed(T0 + timedelta(seconds=241))
    assert feed["tank_gal"] == pytest.approx(47.0 - 4.0 * 239 / 3600, abs=0.01)
    assert feed["range_mi"] is not None
    assert feed["capacity_gal"] == 47.0
    assert feed["mpg_spark"] == pytest.approx([15.0], rel=0.01)
    assert feed["stale"] is False


def test_display_parses_the_feed() -> None:
    state = gauge_from_dict(
        {
            "ts": "2026-11-20T18:00:00Z",
            "tank_gal": 40.0,
            "range_mi": 600.0,
            "capacity_gal": 47.0,
            "stale": False,
            "calibrating": True,
            "mpg_spark": [14.0, 15.5],
        }
    )
    assert state == GaugeState(
        tank_gal=40.0,
        range_mi=600.0,
        capacity_gal=47.0,
        mpg_spark=(14.0, 15.5),
        updated=T0,
        stale=False,
        calibrating=True,
    )


def test_service_stale_flag_makes_the_gauge_stale() -> None:
    state = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0, stale=True)
    assert is_stale(state, T0)


def test_old_feed_means_the_service_is_down() -> None:
    state = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0)
    assert is_stale(state, T0 + timedelta(minutes=6))


def test_calibrating_is_labelled_on_the_frame() -> None:
    base = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0)
    cal = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0, calibrating=True)
    assert render_gauge(base, T0, tz="UTC").tobytes() != render_gauge(cal, T0, tz="UTC").tobytes()


def test_reader_falls_back_to_no_data(tmp_path: Path) -> None:
    path = tmp_path / "gauge.json"
    read = gauge_file_reader(path)
    assert read() == GaugeState(tank_gal=None, range_mi=None)
    path.write_text("{oops")
    assert read() == GaugeState(tank_gal=None, range_mi=None)
    write_state(path, {"ts": "2026-11-20T18:00:00Z", "tank_gal": 40.0, "range_mi": 600.0})
    assert read().tank_gal == 40.0


def test_no_tank_yet_says_no_fuel_data_not_stale() -> None:
    from bigair.display_render import status_text

    state = GaugeState(tank_gal=None, range_mi=None, updated=T0, calibrating=True)
    assert status_text(state, T0, "UTC") == "NO FUEL DATA"


def test_status_texts() -> None:
    from bigair.display_render import status_text

    live = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0)
    assert status_text(live, T0, "UTC") == "LIVE"
    cal = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0, calibrating=True)
    assert status_text(cal, T0, "UTC") == "LIVE · CALIBRATING"
    stale = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0, stale=True)
    assert status_text(stale, T0, "UTC") == "STALE: NO ENGINE DATA"
    down = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0)
    assert status_text(down, T0 + timedelta(minutes=6), "UTC") == "STALE since 18:00"


def test_fill_pending_is_shown_on_the_dash() -> None:
    from bigair.display_render import status_text

    g = gauge_from_dict(
        {
            "ts": "2026-11-20T18:00:00Z",
            "tank_gal": 30.0,
            "range_mi": 400.0,
            "calibrating": True,
            "fill_pending": True,
        }
    )
    assert g.fill_pending
    assert status_text(g, T0, "UTC") == "FILL PENDING · ANSWER ON PHONE"
