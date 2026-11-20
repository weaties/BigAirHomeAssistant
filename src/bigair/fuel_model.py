"""Tank level, gallons used, range and mpg — Critical tier.

Design: .claude/skills/fuel-model/SKILL.md. #7 implements the integrator and
tank level; rolling/instant mpg and range (#8) and fills/persistence (#9) are
still NotImplementedError, so their acceptance tests stay xfail.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from datetime import datetime

    from bigair.signals import FillUp, FuelState, ObdSample

LITRES_PER_GAL = 3.785411784
ULSD_DENSITY_G_PER_L = 840.0  # assumption; corrected by pump-receipt calibration
DIESEL_AFR_STOICH = 14.5  # assumption; corrected by pump-receipt calibration


@dataclass(frozen=True)
class FuelModelConfig:
    capacity_gal: float = 47.0  # nominal; usable capacity UNVERIFIED
    rolling_window_mi: float = 150.0
    rolling_min_mi: float = 50.0
    prior_mpg: float = 15.0
    max_gap_s: float = 5.0
    instant_window_s: float = 30.0
    instant_min_mi: float = 0.05
    instant_cap_mpg: float = 50.0


MAX_RATE_GPH = 40.0  # anything above is a decoding error, not a V6 diesel
STALE_AFTER_S = 60.0  # no valid fuel rate this long with ignition on → stale


class FuelModel:
    """Integrates engine fuel into a persisted tank state."""

    def __init__(self, config: FuelModelConfig, state: FuelState) -> None:
        self.config = config
        self.state = state
        self._last: ObdSample | None = None  # last accepted sample (integration baseline)
        self._last_valid_ts: datetime | None = None  # last sample with a usable fuel rate

    def update(self, sample: ObdSample) -> FuelState:
        """Integrate one sample; return the new state (/fuel-model integration rules)."""
        last = self._last
        if last is not None and sample.ts <= last.ts:
            return self.state  # duplicate or out of order
        rate = sample.fuel_rate_gph
        if rate is not None and not 0.0 <= rate <= MAX_RATE_GPH:
            logger.warning("dropping absurd fuel rate {} gph at {}", rate, sample.ts)
            return self.state

        if not sample.ignition:
            # Engine off: no engine fuel; this sample is the new baseline.
            self._last = sample
            self.state = replace(self.state, ts=sample.ts, stale=False)
            return self.state

        if rate is None:
            # Ignition on but no usable rate: can't integrate. Keep the old
            # baseline so the next valid sample is treated as a gap.
            stale = (
                self._last_valid_ts is not None
                and (sample.ts - self._last_valid_ts).total_seconds() > STALE_AFTER_S
            )
            self.state = replace(self.state, ts=sample.ts, stale=stale)
            return self.state

        added = gap = 0.0
        if last is not None and last.ignition and last.fuel_rate_gph is not None:
            dt = (sample.ts - last.ts).total_seconds()
            if dt <= self.config.max_gap_s:
                added = (last.fuel_rate_gph + rate) / 2 * dt / 3600
            else:
                miles = ((last.speed_mph or 0.0) + (sample.speed_mph or 0.0)) / 2 * dt / 3600
                gap = miles / self._gap_mpg()
                added = gap
        self._last = sample
        self._last_valid_ts = sample.ts
        self.state = replace(
            self.state,
            ts=sample.ts,
            engine_used_gal=self.state.engine_used_gal + added,
            gap_gal=self.state.gap_gal + gap,
            stale=False,
        )
        return self.state

    def _gap_mpg(self) -> float:
        """mpg used to estimate fuel across a data gap. #8 replaces the prior with mpg_rolling."""
        return self.config.prior_mpg

    def apply_fill(self, fill: FillUp) -> FuelState:
        """Reset counters on a confirmed fill (full → capacity, partial → +gallons)."""
        raise NotImplementedError("M1: fill reset")

    @property
    def tank_gal(self) -> float:
        """Gallons at last fill minus engine and heater fuel used since."""
        s = self.state
        return s.gal_after_last_fill - s.engine_used_gal - s.heater_used_gal

    @property
    def mpg_rolling(self) -> float:
        raise NotImplementedError("M1: rolling mpg")

    @property
    def range_mi(self) -> float:
        raise NotImplementedError("M1: range")

    @property
    def mpg_instant_30s(self) -> float | None:
        raise NotImplementedError("M1: instant mpg")

    @property
    def mpg_since_full(self) -> float | None:
        """True mpg of the most recent full-to-full interval, if any."""
        raise NotImplementedError("M1: fill-to-fill mpg")
