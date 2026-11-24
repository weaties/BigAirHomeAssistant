"""gpsd TPV → GpsFix, and when to write track points (#14). Synthetic positions only."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bigair.gps_logger import MPH_PER_MPS, GradeEstimator, TrackRecorder, fix_from_tpv
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


# --- road grade (#78) ---------------------------------------------------------

M_PER_DEG_LAT = 6_371_000 * math.pi / 180  # haversine metres per degree of latitude


def _climb(
    grade_pct: float,
    n: int,
    mps: float = 26.8,
    noise_m: float = 0.0,
    start_s: int = 0,
    alt0: float = 100.0,
) -> list[GpsFix]:
    """1 Hz fixes due north at ``mps`` on a steady grade, alt noise alternating ±noise_m."""
    out = []
    for i in range(start_s, start_s + n):
        d = i * mps
        out.append(
            GpsFix(
                ts=T0 + timedelta(seconds=i),
                lat=45.0 + d / M_PER_DEG_LAT,
                lon=-120.0,
                speed_mph=mps * MPH_PER_MPS,
                alt_m=alt0 + d * grade_pct / 100 + (noise_m if i % 2 else -noise_m),
            )
        )
    return out


def _run(fixes: list[GpsFix], g: GradeEstimator | None = None) -> list[float | None]:
    g = g or GradeEstimator()
    return [g.update(f) for f in fixes]


def test_steady_climb_reads_its_grade() -> None:
    grades = _run(_climb(5.0, 60))
    assert grades[-1] == pytest.approx(5.0, abs=0.05)


def test_descent_is_negative() -> None:
    assert _run(_climb(-3.0, 60))[-1] == pytest.approx(-3.0, abs=0.05)


def test_no_grade_until_the_window_is_covered() -> None:
    """400 m at 26.8 m/s is 15 s: GPS altitude over a shorter distance is mostly noise."""
    grades = _run(_climb(5.0, 30))
    assert all(g is None for g in grades[:14])
    assert all(g is not None for g in grades[16:])


def test_altitude_noise_is_averaged_out_by_the_window() -> None:
    """±3 m of GPS altitude noise over 400 m: within about 1.5 % of the true grade."""
    grades = [g for g in _run(_climb(4.0, 120, noise_m=3.0)) if g is not None]
    assert all(abs(g - 4.0) < 1.6 for g in grades)


def test_a_gap_in_fixes_restarts_the_window() -> None:
    g = GradeEstimator()
    _run(_climb(5.0, 60), g)
    after = _run(_climb(5.0, 5, start_s=120), g)  # 60 s without fixes
    assert after == [None] * 5


def test_parked_has_no_grade_and_jitter_adds_no_distance() -> None:
    g = GradeEstimator()
    parked = [
        GpsFix(T0 + timedelta(seconds=i), 45.0 + (i % 2) * 1e-5, -120.0, 0.3, 100.0 + i % 3)
        for i in range(600)
    ]
    assert set(_run(parked, g)) == {None}
    assert g.distance_m == 0.0


def test_no_altitude_means_no_grade() -> None:
    fixes = [replace(f, alt_m=None) for f in _climb(5.0, 60)]
    assert set(_run(fixes)) == {None}


def test_an_implausible_grade_is_dropped() -> None:
    """A 120 m altitude jump (a multipath glitch) is not a 30 % road."""
    fixes = _climb(0.0, 60)
    fixes[-1] = replace(fixes[-1], alt_m=fixes[-1].alt_m + 120)  # type: ignore[operator]
    assert _run(fixes)[-1] is None
