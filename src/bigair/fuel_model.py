"""Tank level, gallons used, range and mpg — Critical tier.

Design: .claude/skills/fuel-model/SKILL.md. #7 implements the integrator and
tank level, #8 rolling/instant mpg and range, #9 fills and snapshots for the
durable state file (bigair.state_file).
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from bigair.signals import FillStatus, FillType, FuelState

if TYPE_CHECKING:
    from bigair.signals import FillUp, ObdSample

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
MAX_GAP_ESTIMATE_S = 3600.0  # longer gaps aren't estimated: no clock is that trustworthy
CLOCK_STEP_WARN_S = 5.0  # wall and monotonic time disagreeing by more → the clock stepped
STALE_AFTER_S = 60.0  # no valid fuel rate this long with ignition on → stale
ROLLING_CHUNK_MI = 1.0  # rolling-window granularity; the oldest chunk is pro-rated
CHECKPOINT_EVERY_S = 10.0  # counter history, so a late-confirmed fill applies at its own time
CHECKPOINT_KEEP = timedelta(hours=24)
SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class FillResult:
    """What a confirmed fill did, for the fill_up record."""

    tank_before_gal: float
    tank_after_gal: float
    mpg_since_full: float | None


@dataclass(frozen=True)
class _Checkpoint:
    ts: datetime
    engine_used_gal: float
    heater_used_gal: float
    gap_gal: float
    miles_since_full: float | None

    @classmethod
    def of(cls, s: FuelState) -> _Checkpoint:
        return cls(s.ts, s.engine_used_gal, s.heater_used_gal, s.gap_gal, s.miles_since_full)


@dataclass
class _Chunk:
    """Measured (not gap-estimated) driving: miles and engine gallons."""

    miles: float = 0.0
    gal: float = 0.0


class FuelModel:
    """Integrates engine fuel into a persisted tank state."""

    def __init__(self, config: FuelModelConfig, state: FuelState) -> None:
        self.config = config
        self.state = state
        self._last: ObdSample | None = None  # last accepted sample (integration baseline)
        self._last_valid_ts: datetime | None = None  # last sample with a usable fuel rate
        self._rolling: deque[_Chunk] = deque([_Chunk()])  # newest chunk is open, on the right
        self._instant: deque[tuple[datetime, float, float]] = deque()  # (ts, miles, gal) steps
        self._checkpoints: deque[_Checkpoint] = deque([_Checkpoint.of(state)])
        self.last_fill: FillResult | None = None

    def update(self, sample: ObdSample) -> FuelState:
        """Integrate one sample; return the new state (/fuel-model integration rules)."""
        last = self._last
        if last is not None and _seconds_between(last, sample) <= 0:
            return self.state  # duplicate or out of order
        self._trim_instant(sample.ts)
        rate = sample.fuel_rate_gph
        if rate is not None and not 0.0 <= rate <= MAX_RATE_GPH:
            logger.warning("dropping absurd fuel rate {} gph at {}", rate, sample.ts)
            return self.state

        if not sample.ignition:
            # Engine off: no engine fuel; this sample is the new baseline.
            self._last = sample
            self.state = replace(self.state, ts=sample.ts, stale=False)
            self._checkpoint(force=True)  # fills happen while parked
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

        added = gap = miles = 0.0
        if last is not None and last.ignition and last.fuel_rate_gph is not None:
            dt = _seconds_between(last, sample)
            miles = ((last.speed_mph or 0.0) + (sample.speed_mph or 0.0)) / 2 * dt / 3600
            if dt > MAX_GAP_ESTIMATE_S:
                # Backstop (#107): an hour-plus gap with the ignition on is a clock or
                # data fault, not driving. Count nothing rather than guess.
                logger.warning("not estimating a {:.0f} s gap at {}", dt, sample.ts)
                miles = 0.0
            elif dt <= self.config.max_gap_s:
                added = (last.fuel_rate_gph + rate) / 2 * dt / 3600
                self._record(sample.ts, miles, added)
            else:
                # Estimated, not measured: kept out of the mpg windows so they
                # can't reinforce their own estimate.
                gap = miles / self._gap_mpg()
                added = gap
        self._last = sample
        self._last_valid_ts = sample.ts
        since_full = self.state.miles_since_full
        self.state = replace(
            self.state,
            ts=sample.ts,
            engine_used_gal=self.state.engine_used_gal + added,
            gap_gal=self.state.gap_gal + gap,
            miles_since_full=None if since_full is None else since_full + miles,
            stale=False,
        )
        self._checkpoint()
        return self.state

    def _checkpoint(self, force: bool = False) -> None:
        cp = _Checkpoint.of(self.state)
        newest = self._checkpoints[-1]
        if cp == newest:
            return
        if force or (cp.ts - newest.ts).total_seconds() >= CHECKPOINT_EVERY_S:
            self._checkpoints.append(cp)
        while self._checkpoints[0].ts < cp.ts - CHECKPOINT_KEEP:
            self._checkpoints.popleft()

    def _counters_at(self, ts: datetime) -> _Checkpoint:
        """Counters as they stood at `ts` (the latest checkpoint at or before it)."""
        if ts >= self.state.ts:
            return _Checkpoint.of(self.state)
        found = [cp for cp in self._checkpoints if cp.ts <= ts]
        if not found:
            logger.warning("fill at {} predates the counter history; applying it now", ts)
            return _Checkpoint.of(self.state)
        return found[-1]

    def _record(self, ts: datetime, miles: float, gal: float) -> None:
        """Add one measured integration step to the rolling and instant windows."""
        self._instant.append((ts, miles, gal))
        chunk = self._rolling[-1]
        chunk.miles += miles
        chunk.gal += gal
        if chunk.miles >= ROLLING_CHUNK_MI:
            self._rolling.append(_Chunk())
        window = self.config.rolling_window_mi
        while sum(c.miles for c in self._rolling) - self._rolling[0].miles >= window:
            self._rolling.popleft()

    def _trim_instant(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.config.instant_window_s)
        while self._instant and self._instant[0][0] <= cutoff:
            self._instant.popleft()

    def _gap_mpg(self) -> float:
        """mpg used to estimate fuel across a data gap."""
        return self.mpg_rolling

    def apply_fill(self, fill: FillUp) -> FuelState:
        """Reset counters on a confirmed fill (full → capacity, partial → +gallons).

        Applied as of `fill.ts`: fuel burned between the pump and Dan's
        confirmation still counts against the new fill.
        """
        s = self.state
        if fill.status is not FillStatus.CONFIRMED or fill.fill_id == s.last_fill_id:
            return s
        if fill.fill_type is FillType.PARTIAL and fill.gallons is None:
            logger.warning("partial fill {} has no gallons; ignored", fill.fill_id)
            return s
        at = self._counters_at(fill.ts)
        tank_before = s.gal_after_last_fill - at.engine_used_gal - at.heater_used_gal
        cap = self.config.capacity_gal
        after_miles = (
            None
            if s.miles_since_full is None or at.miles_since_full is None
            else s.miles_since_full - at.miles_since_full
        )
        mpg: float | None = None
        if fill.fill_type is FillType.FULL:
            gal_after = cap
            pump = s.pump_gal_since_full + (fill.gallons or 0.0)
            known = at.miles_since_full is not None and at.miles_since_full > 0
            if known and fill.gallons is not None and pump > 0:
                assert at.miles_since_full is not None
                mpg = at.miles_since_full / pump
            miles_since_full: float | None = after_miles if after_miles is not None else 0.0
            pump_since_full = 0.0
            mpg_since_full = mpg if mpg is not None else s.mpg_since_full
        else:
            assert fill.gallons is not None
            gal_after = max(tank_before, 0.0) + fill.gallons
            if gal_after > cap:
                logger.warning(
                    "partial fill {} would overfill ({:.2f} gal); capped at {}",
                    fill.fill_id,
                    gal_after,
                    cap,
                )
                gal_after = cap
            miles_since_full = s.miles_since_full
            pump_since_full = s.pump_gal_since_full + fill.gallons
            mpg_since_full = s.mpg_since_full
        self.state = replace(
            s,
            gal_after_last_fill=gal_after,
            engine_used_gal=s.engine_used_gal - at.engine_used_gal,
            heater_used_gal=s.heater_used_gal - at.heater_used_gal,
            gap_gal=s.gap_gal - at.gap_gal,
            last_fill_type=fill.fill_type,
            miles_since_full=miles_since_full,
            pump_gal_since_full=pump_since_full,
            mpg_since_full=mpg_since_full,
            last_fill_id=fill.fill_id,
        )
        self._checkpoints = deque([_Checkpoint.of(self.state)])
        self.last_fill = FillResult(tank_before, gal_after, mpg)
        return self.state

    def snapshot(self) -> dict[str, Any]:
        """Everything a restart needs, as JSON-ready data for bigair.state_file."""
        state = asdict(self.state)
        state["ts"] = self.state.ts.isoformat()
        state["last_fill_type"] = self.state.last_fill_type.value
        return {
            "version": SNAPSHOT_VERSION,
            "state": state,
            "rolling": [[c.miles, c.gal] for c in self._rolling],
        }

    @classmethod
    def from_snapshot(cls, config: FuelModelConfig, data: dict[str, Any]) -> FuelModel:
        """Restore from snapshot(). The first sample after a restart is a new baseline."""
        if data.get("version") != SNAPSHOT_VERSION:
            raise ValueError(f"unsupported fuel snapshot version {data.get('version')!r}")
        raw = dict(data["state"])
        raw["ts"] = datetime.fromisoformat(raw["ts"])
        raw["last_fill_type"] = FillType(raw["last_fill_type"])
        model = cls(config, FuelState(**raw))
        model._rolling = deque(_Chunk(miles, gal) for miles, gal in data["rolling"])
        if not model._rolling:
            model._rolling.append(_Chunk())
        return model

    @property
    def tank_gal(self) -> float:
        """Gallons at last fill minus engine and heater fuel used since."""
        s = self.state
        return s.gal_after_last_fill - s.engine_used_gal - s.heater_used_gal

    def _rolling_window(self) -> tuple[float, float]:
        """(miles, gallons) in the most recent rolling_window_mi of measured driving."""
        window = self.config.rolling_window_mi
        chunks = list(self._rolling)
        rest_mi = sum(c.miles for c in chunks[1:])
        oldest = chunks[0]
        # Pro-rate the oldest chunk so the window holds at most `window` miles.
        share = 1.0 if oldest.miles == 0 else min(1.0, max(0.0, window - rest_mi) / oldest.miles)
        miles = rest_mi + oldest.miles * share
        gal = sum(c.gal for c in chunks[1:]) + oldest.gal * share
        return miles, gal

    @property
    def mpg_rolling_measured(self) -> bool:
        """True once mpg_rolling comes from driving, not the fallback (≥ rolling_min_mi)."""
        miles, gal = self._rolling_window()
        return miles >= self.config.rolling_min_mi and gal > 0

    @property
    def mpg_rolling(self) -> float:
        """Miles ÷ engine gallons over the most recent rolling_window_mi of driving.

        Doesn't reset at fills. Below rolling_min_mi of history, falls back to
        the latest full-to-full mpg, else the configured prior.
        """
        miles, gal = self._rolling_window()
        if miles < self.config.rolling_min_mi or gal <= 0:
            return self.state.mpg_since_full or self.config.prior_mpg
        return miles / gal

    @property
    def range_mi(self) -> float:
        """Tank (never below zero) × rolling mpg."""
        return max(self.tank_gal, 0.0) * self.mpg_rolling

    @property
    def mpg_instant_30s(self) -> float | None:
        """Σmiles ÷ Σengine gallons over the trailing 30 s; None when (nearly) stopped."""
        miles = sum(m for _, m, _ in self._instant)
        if miles < self.config.instant_min_mi:
            return None
        gal = sum(g for _, _, g in self._instant)
        cap = self.config.instant_cap_mpg
        return cap if gal <= 0 else min(miles / gal, cap)

    @property
    def mpg_since_full(self) -> float | None:
        """True mpg of the most recent full-to-full interval, if any."""
        return self.state.mpg_since_full


def _seconds_between(a: ObdSample, b: ObdSample) -> float:
    """Seconds from sample a to b: monotonic when both have it (#107), else wall clock.

    After a cold boot the Pi's wall clock can be days behind until chrony steps it
    from GPS. Measured on the wall clock, that step looked like days of driving.
    """
    wall = (b.ts - a.ts).total_seconds()
    if a.mono_s is None or b.mono_s is None:
        return wall
    mono = b.mono_s - a.mono_s
    if abs(wall - mono) > CLOCK_STEP_WARN_S:
        logger.warning("wall clock stepped {:+.0f} s between samples at {}", wall - mono, b.ts)
    return mono
