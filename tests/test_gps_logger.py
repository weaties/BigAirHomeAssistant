"""gpsd TPV → GpsFix, and when to write track points (#14). Synthetic positions only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.gps_logger import TrackRecorder, fix_from_tpv
from bigair.signals import GpsFix

# Shaped like a real gpsd 3.25 TPV from the XGPS160 (fields trimmed); position synthetic.
TPV = {
    "class": "TPV",
    "device": "/dev/rfcomm0",
    "mode": 3,
    "time": "2026-11-21T15:00:00.000Z",
    "ept": 0.005,
    "lat": 39.5,
    "lon": -111.0,
    "altHAE": 1510.2,
    "altMSL": 1528.4,
    "alt": 1528.4,
    "track": 87.3,
    "magtrack": 98.1,
    "speed": 26.82,
    "climb": 0.1,
    "eph": 2.9,
}


def test_fix_from_a_3d_tpv() -> None:
    fix = fix_from_tpv(TPV)
    assert fix == GpsFix(
        ts=datetime(2026, 11, 21, 15, 0, tzinfo=UTC),
        lat=39.5,
        lon=-111.0,
        speed_mph=pytest.approx(60.0, abs=0.01),
        alt_m=1528.4,
        heading_deg=87.3,
        hdop=None,
    )


def test_2d_fix_has_no_altitude() -> None:
    fix = fix_from_tpv({**TPV, "mode": 2, "altMSL": None, "alt": None})
    assert fix is not None and fix.alt_m is None


@pytest.mark.parametrize(
    "report",
    [
        {**TPV, "mode": 1},  # no fix
        {**TPV, "mode": 0},
        {k: v for k, v in TPV.items() if k != "lat"},
        {k: v for k, v in TPV.items() if k != "time"},
        {**TPV, "class": "SKY"},
        {**TPV, "time": "garbage"},
    ],
)
def test_no_usable_fix(report: dict[str, object]) -> None:
    assert fix_from_tpv(report) is None


T0 = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)


def _fix(sec: float, mph: float) -> GpsFix:
    return GpsFix(ts=T0 + timedelta(seconds=sec), lat=39.5, lon=-111.0, speed_mph=mph)


def test_every_5_s_while_moving() -> None:
    rec = TrackRecorder()
    kept = [f for f in (_fix(i * 0.1, 40.0) for i in range(300)) if rec.keep(f)]  # 30 s at 10 Hz
    assert len(kept) == 6
    assert [round((k.ts - T0).total_seconds(), 1) for k in kept[:2]] == [0.0, 5.0]


def test_once_a_minute_when_parked() -> None:
    rec = TrackRecorder()
    kept = [f for f in (_fix(i, 0.0) for i in range(600)) if rec.keep(f)]  # 10 min parked
    assert len(kept) == 10


def test_moving_off_is_kept_at_once() -> None:
    rec = TrackRecorder()
    assert rec.keep(_fix(0, 0.0))
    assert not rec.keep(_fix(10, 0.0))
    assert rec.keep(_fix(11, 20.0))  # started moving: don't wait for the parked minute
