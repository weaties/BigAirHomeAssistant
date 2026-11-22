"""Current-trip mpg buckets for the dash sparkline (#17).

A trip starts when the engine runs again after a long park. Every
`bucket_mi` of measured driving closes a bucket with its own mpg. Gaps in the
data are never bridged (the fuel model estimates those; a sparkline doesn't
need to). Pure: ObdSample in, buckets out.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bigair.signals import ObdSample

MAX_STEP_S = 5.0
EPS_MI = 1e-6


class TripMpg:
    def __init__(
        self,
        bucket_mi: float = 2.0,
        max_buckets: int = 40,
        new_trip_after: timedelta = timedelta(minutes=30),
        cap_mpg: float = 50.0,
    ) -> None:
        self.bucket_mi = bucket_mi
        self.new_trip_after = new_trip_after
        self.cap_mpg = cap_mpg
        self._buckets: deque[float] = deque(maxlen=max_buckets)
        self._miles = 0.0
        self._gal = 0.0
        self._trip_miles = 0.0
        self._trip_gal = 0.0
        self._last: ObdSample | None = None  # last ignition-on sample with speed and rate
        self.trip_id: str | None = None  # UTC start of the current trip, e.g. 2026-11-21T15:00Z
        self.trip_start: datetime | None = None  # UTC time of the trip's first sample

    @property
    def buckets(self) -> tuple[float, ...]:
        return tuple(self._buckets)

    @property
    def trip_miles(self) -> float:
        return self._trip_miles

    @property
    def trip_gal(self) -> float:
        return self._trip_gal

    @property
    def trip_mpg(self) -> float | None:
        """Whole-trip average, once there's at least half a mile of it."""
        if self._trip_miles < 0.5:
            return None
        if self._trip_gal <= 0:
            return self.cap_mpg
        return min(self._trip_miles / self._trip_gal, self.cap_mpg)

    def update(self, sample: ObdSample) -> None:
        if not sample.ignition or sample.speed_mph is None or sample.fuel_rate_gph is None:
            return
        last, self._last = self._last, sample
        if last is None:
            self._start(sample.ts)
            return
        gap = sample.ts - last.ts
        if gap > self.new_trip_after:
            self._buckets.clear()
            self._miles = self._gal = self._trip_miles = self._trip_gal = 0.0
            self._start(sample.ts)
            return
        dt = gap.total_seconds()
        if dt <= 0 or dt > MAX_STEP_S:
            return
        assert last.speed_mph is not None and last.fuel_rate_gph is not None
        miles = (last.speed_mph + sample.speed_mph) / 2 * dt / 3600
        gal = (last.fuel_rate_gph + sample.fuel_rate_gph) / 2 * dt / 3600
        self._miles += miles
        self._gal += gal
        self._trip_miles += miles
        self._trip_gal += gal
        if self._miles >= self.bucket_mi - EPS_MI:
            mpg = self.cap_mpg if self._gal <= 0 else min(self._miles / self._gal, self.cap_mpg)
            self._buckets.append(mpg)
            self._miles = self._gal = 0.0

    def _start(self, ts: datetime) -> None:
        self.trip_id = ts.strftime("%Y-%m-%dT%H:%MZ")
        self.trip_start = ts
