"""Trip mpg buckets for the dash sparkline (#17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.signals import ObdSample
from bigair.trip import TripMpg

T0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


def _s(
    sec: float, mph: float | None = 60.0, gph: float | None = 4.0, ignition: bool = True
) -> ObdSample:
    return ObdSample(
        ts=T0 + timedelta(seconds=sec), ignition=ignition, speed_mph=mph, fuel_rate_gph=gph
    )


def _drive(t: TripMpg, start: int, seconds: int, mph: float, gph: float) -> int:
    for i in range(seconds + 1):
        t.update(_s(start + i, mph, gph))
    return start + seconds


def test_buckets_every_two_miles() -> None:
    trip = TripMpg(bucket_mi=2.0)
    _drive(trip, 0, 360, 60.0, 4.0)  # 6 mi at 15 mpg
    assert trip.buckets == pytest.approx((15.0, 15.0, 15.0), rel=1e-3)


def test_partial_bucket_is_not_shown_yet() -> None:
    trip = TripMpg(bucket_mi=2.0)
    _drive(trip, 0, 60, 60.0, 4.0)  # 1 mi
    assert trip.buckets == ()


def test_buckets_follow_changing_economy() -> None:
    trip = TripMpg(bucket_mi=2.0)
    t = _drive(trip, 0, 120, 60.0, 6.0)  # 2 mi at 10 mpg
    _drive(trip, t + 1, 120, 60.0, 3.0)  # 2 mi at 20 mpg
    assert trip.buckets[0] == pytest.approx(10.0, rel=0.01)
    assert trip.buckets[-1] == pytest.approx(20.0, rel=0.02)


def test_keeps_only_the_latest_buckets() -> None:
    trip = TripMpg(bucket_mi=1.0, max_buckets=5)
    _drive(trip, 0, 600, 60.0, 4.0)  # 10 mi
    assert len(trip.buckets) == 5


def test_a_long_park_starts_a_new_trip() -> None:
    trip = TripMpg(bucket_mi=2.0, new_trip_after=timedelta(minutes=30))
    t = _drive(trip, 0, 240, 60.0, 4.0)
    trip.update(_s(t + 1, None, None, ignition=False))
    _drive(trip, t + 3600, 10, 60.0, 4.0)
    assert trip.buckets == ()


def test_a_short_stop_keeps_the_trip() -> None:
    trip = TripMpg(bucket_mi=2.0, new_trip_after=timedelta(minutes=30))
    t = _drive(trip, 0, 240, 60.0, 4.0)
    trip.update(_s(t + 1, None, None, ignition=False))
    _drive(trip, t + 300, 10, 60.0, 4.0)
    assert len(trip.buckets) == 2


def test_gaps_are_not_bridged() -> None:
    trip = TripMpg(bucket_mi=2.0)
    trip.update(_s(0))
    trip.update(_s(600))  # 10 min with no data: not 10 miles of 15 mpg
    assert trip.buckets == ()


def test_overrun_bucket_is_capped() -> None:
    trip = TripMpg(bucket_mi=1.0, cap_mpg=50.0)
    _drive(trip, 0, 60, 60.0, 0.0)  # coasting 1 mi, no fuel
    assert trip.buckets == (50.0,)


def test_trip_totals_and_start() -> None:
    trip = TripMpg(bucket_mi=2.0)
    assert trip.trip_miles == 0.0 and trip.trip_gal == 0.0 and trip.trip_start is None
    _drive(trip, 0, 360, 60.0, 4.0)  # 6 mi, 0.4 gal
    assert trip.trip_miles == pytest.approx(6.0, rel=1e-3)
    assert trip.trip_gal == pytest.approx(0.4, rel=1e-3)
    assert trip.trip_start == T0


def test_new_trip_resets_totals_and_start() -> None:
    trip = TripMpg(bucket_mi=2.0)
    _drive(trip, 0, 360, 60.0, 4.0)
    restart = 360 + 31 * 60
    _drive(trip, restart, 60, 60.0, 4.0)  # 1 mi after a 31 min park
    assert trip.trip_miles == pytest.approx(1.0, rel=1e-3)
    assert trip.trip_start == T0 + timedelta(seconds=restart)
