"""Fuel economy on the dash gauge: rolling (measured only), instant, last fill, trip."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.display_render import GaugeState, gauge_from_dict, mpg_lines, render_gauge
from bigair.fuel_model import FuelModel, FuelModelConfig
from bigair.service import FuelService, ServiceConfig
from bigair.signals import FillType, FuelState, ObdSample
from bigair.trip import TripMpg

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 20, 19, 0, tzinfo=UTC)


def _s(sec: float, mph: float = 60.0, gph: float = 4.0) -> ObdSample:
    return ObdSample(
        ts=T0 + timedelta(seconds=sec), ignition=True, speed_mph=mph, fuel_rate_gph=gph
    )


def _model() -> FuelModel:
    st = FuelState(
        ts=T0,
        gal_after_last_fill=47.0,
        engine_used_gal=0.0,
        heater_used_gal=0.0,
        last_fill_type=FillType.FULL,
    )
    return FuelModel(FuelModelConfig(), st)


def test_rolling_mpg_is_marked_measured_only_after_the_minimum_miles() -> None:
    m = _model()
    for i in range(1, 1801):  # 30 mi
        m.update(_s(i))
    assert not m.mpg_rolling_measured
    for i in range(1801, 3601):  # 60 mi total
        m.update(_s(i))
    assert m.mpg_rolling_measured


def test_trip_average() -> None:
    trip = TripMpg(bucket_mi=2.0)
    for i in range(121):
        trip.update(_s(i, gph=6.0))  # 2 mi at 10 mpg
    for i in range(121, 242):
        trip.update(_s(i, gph=3.0))  # 2 mi at 20 mpg
    assert trip.trip_mpg == pytest.approx(4.0 / 0.3, rel=0.01)  # 4 mi on 0.3 gal


def test_trip_average_needs_some_distance() -> None:
    trip = TripMpg()
    for i in range(10):
        trip.update(_s(i))
    assert trip.trip_mpg is None


def test_feed_carries_economy(tmp_path: Path) -> None:
    svc = FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))
    svc.handle_manual_full(T0)
    for i in range(1, 3601):
        svc.handle_sample(_s(i))
    feed = svc.gauge_feed(T0 + timedelta(seconds=3601))
    assert feed["mpg_rolling"] == pytest.approx(15.0, rel=0.01)
    assert feed["mpg_instant_30s"] == pytest.approx(15.0, rel=0.01)
    assert feed["trip_mpg"] == pytest.approx(15.0, rel=0.01)
    assert feed["mpg_since_full"] is None


def test_feed_hides_the_prior_as_rolling_mpg(tmp_path: Path) -> None:
    svc = FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))
    svc.handle_manual_full(T0)
    for i in range(1, 61):
        svc.handle_sample(_s(i))
    assert svc.gauge_feed(T0 + timedelta(seconds=61))["mpg_rolling"] is None


def test_display_parses_economy() -> None:
    g = gauge_from_dict(
        {
            "ts": "2026-11-20T19:00:00Z",
            "mpg_rolling": 14.9,
            "mpg_instant_30s": 15.5,
            "mpg_since_full": 14.2,
            "trip_mpg": 13.8,
        }
    )
    assert (g.mpg_rolling, g.mpg_instant, g.mpg_since_full, g.trip_mpg) == (14.9, 15.5, 14.2, 13.8)


def test_mpg_lines() -> None:
    g = GaugeState(
        tank_gal=40.0,
        range_mi=600.0,
        mpg_rolling=14.94,
        mpg_instant=15.46,
        mpg_since_full=14.2,
        trip_mpg=13.8,
    )
    assert mpg_lines(g) == ("14.9", "150 mi avg", "now 15.5 · trip 13.8 · fill 14.2")


def test_mpg_lines_before_anything_is_measured() -> None:
    g = GaugeState(tank_gal=40.0, range_mi=600.0)
    assert mpg_lines(g) == ("--", "avg after 50 mi", "now -- · trip -- · fill --")


def test_economy_block_changes_the_frame() -> None:
    base = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0)
    econ = GaugeState(tank_gal=40.0, range_mi=600.0, updated=T0, mpg_rolling=14.9)
    assert render_gauge(base, T0, tz="UTC").tobytes() != render_gauge(econ, T0, tz="UTC").tobytes()
