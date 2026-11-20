"""GPS track logging (#14): gpsd TPV → GpsFix, and which fixes become track points.

The gpsd client itself is the edge (`gpsd_io.py`). This module is pure: a
TPV report in, a GpsFix out; the recorder keeps one fix every 5 s while
moving and one a minute while parked, so a year of driving stays small.
"""

from __future__ import annotations

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
