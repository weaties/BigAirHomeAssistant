"""GPS track logging (#14): gpsd TPV → GpsFix, and which fixes become track points.

The gpsd client itself is the edge (`gpsd_io.py`). This module is pure: a
TPV report in, a GpsFix out; the recorder keeps one fix every 5 s while
moving and one a minute while parked, so a year of driving stays small.

Road grade (#78) comes from GPS altitude over distance. Altitude from a
consumer GPS is noisy (a few metres from fix to fix), so grade over 5 s is
mostly noise: on real drives its 99th percentile was ±11 %. Measured over a
400 m sliding window it falls within about ±5 % on Seattle hills. Grade is
None, never a guess, until the window is covered, while parked, after a gap,
without a 3D fix, or when the result is implausible for a road.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from bigair.signals import GpsFix

MPH_PER_MPS = 2.2369362920544
MOVING_MPH = 2.0


def _num(report: dict[str, Any], key: str) -> float | None:
    v = report.get(key)
    return float(v) if isinstance(v, int | float) else None


def fix_from_tpv(report: dict[str, Any]) -> GpsFix | None:
    """Convert a gpsd TPV report to a GpsFix (None without a 2D/3D fix)."""
    if report.get("class") != "TPV" or (report.get("mode") or 0) < 2:
        return None
    lat, lon = _num(report, "lat"), _num(report, "lon")
    if lat is None or lon is None:
        return None
    try:
        ts = datetime.fromisoformat(str(report["time"]).replace("Z", "+00:00")).astimezone(UTC)
    except (KeyError, ValueError):
        return None
    speed = _num(report, "speed")
    alt = _num(report, "altMSL")
    if alt is None:
        alt = _num(report, "alt")
    return GpsFix(
        ts=ts,
        lat=lat,
        lon=lon,
        speed_mph=None if speed is None else speed * MPH_PER_MPS,
        alt_m=alt if report.get("mode") == 3 else None,
        heading_deg=_num(report, "track"),
        hdop=None,  # HDOP comes in SKY reports, not TPV
    )


class TrackRecorder:
    def __init__(
        self,
        moving_every: timedelta = timedelta(seconds=5),
        parked_every: timedelta = timedelta(minutes=1),
    ) -> None:
        self.moving_every = moving_every
        self.parked_every = parked_every
        self._last_ts: datetime | None = None
        self._last_moving: bool | None = None

    def keep(self, fix: GpsFix) -> bool:
        """True if this fix should be written as a track point."""
        moving = (fix.speed_mph or 0.0) > MOVING_MPH
        due = (
            self._last_ts is None
            or moving != self._last_moving
            or fix.ts - self._last_ts >= (self.moving_every if moving else self.parked_every)
        )
        if due:
            self._last_ts = fix.ts
            self._last_moving = moving
        return due


EARTH_R_M = 6_371_000.0


def distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle (haversine) distance in metres."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(h))


class GradeEstimator:
    """Road grade in percent over the last ``window_m`` metres driven."""

    def __init__(
        self,
        window_m: float = 400.0,
        step_m: float = 10.0,
        max_gap: timedelta = timedelta(seconds=30),
        max_abs_pct: float = 20.0,
    ) -> None:
        self.window_m = window_m
        self.step_m = step_m
        self.max_gap = max_gap
        self.max_abs_pct = max_abs_pct
        self.distance_m = 0.0
        self._last: GpsFix | None = None
        self._hist: deque[tuple[float, float]] = deque()  # (distance_m, alt_m)

    def _reset(self) -> None:
        self._last = None
        self._hist.clear()

    def update(self, fix: GpsFix) -> float | None:
        """Feed every fix; returns the grade at this fix, or None."""
        moving = (fix.speed_mph or 0.0) > MOVING_MPH
        if fix.alt_m is None or (self._last is not None and fix.ts - self._last.ts > self.max_gap):
            self._reset()
        if fix.alt_m is None or not moving:
            # Parked: GPS jitter would add phantom distance; resume from here.
            if self._last is not None:
                self._last = fix
            return None
        if self._last is not None:
            self.distance_m += distance_m(self._last.lat, self._last.lon, fix.lat, fix.lon)
        self._last = fix
        if not self._hist or self.distance_m - self._hist[-1][0] >= self.step_m:
            self._hist.append((self.distance_m, fix.alt_m))
        # Keep one anchor at or beyond the window, drop anything older.
        while len(self._hist) > 1 and self.distance_m - self._hist[1][0] >= self.window_m:
            self._hist.popleft()
        d0, a0 = self._hist[0]
        run = self.distance_m - d0
        if run < self.window_m:
            return None
        grade = 100.0 * (fix.alt_m - a0) / run
        return grade if abs(grade) <= self.max_abs_pct else None
