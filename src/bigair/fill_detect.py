"""Fill-up detection state machine — Critical tier.

Design: .claude/skills/fill-up-events/SKILL.md. Compares the last settled
level before a park with the first settled level after ignition on. The
level comes from the stock sender, which is a delta detector only (Decision
#31): it's mapped to gallon-equivalents through `level_table`, never shown
as the tank reading. Samples without a level (no VERIFIED source) simply
never produce a candidate.
"""

from __future__ import annotations

import uuid
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from typing import TYPE_CHECKING

from bigair.signals import FillCandidate

if TYPE_CHECKING:
    from datetime import datetime

    from bigair.signals import GpsFix, ObdSample


class DetectorState(StrEnum):
    DRIVING = "driving"
    PARKED = "parked"
    SETTLING = "settling"


# Sender percent → fraction of capacity, piecewise linear. Linear until a
# table is learned from fill-ups (sender before/after vs pump gallons).
LINEAR_TABLE: tuple[tuple[float, float], ...] = ((0.0, 0.0), (100.0, 1.0))


@dataclass(frozen=True)
class FillDetectorConfig:
    capacity_gal: float = 47.0
    min_jump_gal: float = 2.0
    min_jump_fraction: float = 0.05
    park_after_s: float = 60.0
    settle_s: float = 45.0
    baseline_window_s: float = 30.0
    level_table: tuple[tuple[float, float], ...] = LINEAR_TABLE


def level_to_gal(pct: float, table: tuple[tuple[float, float], ...], capacity: float) -> float:
    """Map a sender reading through the calibration table (clamped at its ends)."""
    xs = [x for x, _ in table]
    if pct <= xs[0]:
        return table[0][1] * capacity
    if pct >= xs[-1]:
        return table[-1][1] * capacity
    i = bisect_right(xs, pct)
    (x0, y0), (x1, y1) = table[i - 1], table[i]
    return (y0 + (y1 - y0) * (pct - x0) / (x1 - x0)) * capacity


class FillDetector:
    def __init__(self, config: FillDetectorConfig) -> None:
        self.config = config
        self.state = DetectorState.DRIVING
        self._levels: deque[tuple[datetime, float]] = deque()  # ignition-on, gallon-equivalents
        self._stopped_since: datetime | None = None
        self._was_on = False
        self._before_gal: float | None = None
        self._parked_fix: GpsFix | None = None
        self._settle_start: datetime | None = None
        self._settle_levels: list[tuple[datetime, float]] = []

    @property
    def threshold_gal(self) -> float:
        c = self.config
        return max(c.min_jump_gal, c.min_jump_fraction * c.capacity_gal)

    def update(self, sample: ObdSample, fix: GpsFix | None = None) -> FillCandidate | None:
        """Feed one sample; return a candidate exactly once per detected fill."""
        was_on, self._was_on = self._was_on, sample.ignition
        gal = self._gal(sample)
        if sample.ignition and gal is not None:
            self._remember(sample.ts, gal)

        if self.state is DetectorState.DRIVING:
            self._while_driving(sample)
        elif self.state is DetectorState.PARKED:
            if not sample.ignition and fix is not None:
                self._parked_fix = fix
            if sample.ignition and not was_on:
                self._start_settling(sample.ts)
            elif sample.ignition and _moving(sample):
                self._reset()  # parked with the engine running, then drove off
        elif self.state is DetectorState.SETTLING:
            return self._while_settling(sample, gal, fix)
        return None

    def _gal(self, sample: ObdSample) -> float | None:
        if sample.fuel_level_pct is None:
            return None
        c = self.config
        return level_to_gal(sample.fuel_level_pct, c.level_table, c.capacity_gal)

    def _remember(self, ts: datetime, gal: float) -> None:
        self._levels.append((ts, gal))
        while (ts - self._levels[0][0]).total_seconds() >= self.config.baseline_window_s:
            self._levels.popleft()

    def _while_driving(self, sample: ObdSample) -> None:
        if not sample.ignition:
            self._park()
            return
        if sample.speed_mph is None or _moving(sample):
            self._stopped_since = None
            return
        if self._stopped_since is None:
            self._stopped_since = sample.ts
        elif (sample.ts - self._stopped_since).total_seconds() >= self.config.park_after_s:
            self._park()

    def _park(self) -> None:
        self.state = DetectorState.PARKED
        self._before_gal = median(g for _, g in self._levels) if self._levels else None
        self._stopped_since = None
        self._parked_fix = None

    def _start_settling(self, ts: datetime) -> None:
        self.state = DetectorState.SETTLING
        self._settle_start = ts
        self._settle_levels = []

    def _while_settling(
        self, sample: ObdSample, gal: float | None, fix: GpsFix | None
    ) -> FillCandidate | None:
        if not sample.ignition:
            # Ignition cycled at the pump: keep the original baseline.
            self.state = DetectorState.PARKED
            return None
        if _moving(sample):
            self._reset()  # moved off before the level settled
            return None
        if gal is not None:
            self._settle_levels.append((sample.ts, gal))
        assert self._settle_start is not None
        if (sample.ts - self._settle_start).total_seconds() < self.config.settle_s:
            return None
        window_start = sample.ts.timestamp() - self.config.baseline_window_s
        recent = [g for ts, g in self._settle_levels if ts.timestamp() > window_start]
        before, where = self._before_gal, self._parked_fix or fix
        self._reset()
        if before is None or not recent:
            return None
        delta = median(recent) - before
        if delta < self.threshold_gal:
            return None
        return FillCandidate(
            fill_id=str(uuid.uuid4()),
            ts=sample.ts,
            est_gallons=delta,
            odometer_mi=sample.odometer_mi,
            lat=where.lat if where else None,
            lon=where.lon if where else None,
        )

    def _reset(self) -> None:
        self.state = DetectorState.DRIVING
        self._before_gal = None
        self._parked_fix = None
        self._settle_start = None
        self._settle_levels = []
        self._stopped_since = None


def _moving(sample: ObdSample) -> bool:
    return sample.speed_mph is not None and sample.speed_mph > 0.0
