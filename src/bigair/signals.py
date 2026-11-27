"""Frozen dataclasses shared by every module — decode early, store clean.

Edges (MQTT, gpsd, W-Bus) convert raw payloads into these types with units
already converted; logic modules only ever see these. Every numeric field
name ends in its unit (see CLAUDE.md → Data model).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class FillType(StrEnum):
    FULL = "full"
    PARTIAL = "partial"


class FillSource(StrEnum):
    LIVE = "live"
    MANUAL = "manual"
    HISTORICAL = "historical"


class FillStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ObdSample:
    """One decoded vehicle sample. OBD fields are None when the ECU is asleep or
    the signal is not VERIFIED in docs/pid-register.md."""

    ts: datetime  # UTC
    ignition: bool
    speed_mph: float | None
    fuel_rate_gph: float | None
    odometer_mi: float | None = None
    fuel_level_pct: float | None = None
    battery_v: float | None = None  # WiCAN's own ATRV reading at OBD pin 16
    # Slow-polled (#79): only present on the rounds that asked for them.
    coolant_temp_c: float | None = None
    oil_temp_c: float | None = None
    ambient_temp_c: float | None = None
    # time.monotonic() when the edge read it (#107). Time between samples comes from
    # this, because the wall clock steps when chrony syncs after a cold boot.
    mono_s: float | None = None


@dataclass(frozen=True)
class VictronSample:
    """One decoded Victron Instant Readout record (#109). None = not available.

    `device` is the Influx tag: bmv (battery monitor), mppt (solar), dcdc (Orion).
    `voltage_v`/`current_a` are the battery side (DC/DC: output voltage).
    """

    ts: datetime  # UTC
    mono_s: float  # time.monotonic() at the edge, for freshness and throttling (#107)
    device: str
    voltage_v: float | None = None
    current_a: float | None = None
    power_w: float | None = None  # BMV: voltage × current (not in the record)
    soc_pct: float | None = None
    consumed_ah: float | None = None  # ≤ 0, Victron's sign
    time_to_go_s: int | None = None
    alarm: int | None = None
    aux_voltage_v: float | None = None  # BMV aux input 0: starter battery
    midpoint_voltage_v: float | None = None
    temperature_c: float | None = None
    pv_power_w: int | None = None
    yield_today_wh: float | None = None
    load_current_a: float | None = None
    state: str | None = None
    charger_error: int | None = None
    input_voltage_v: float | None = None
    off_reason: int | None = None


@dataclass(frozen=True)
class GpsFix:
    ts: datetime  # UTC
    lat: float
    lon: float
    speed_mph: float | None = None
    alt_m: float | None = None
    heading_deg: float | None = None
    hdop: float | None = None


@dataclass(frozen=True)
class HeaterSample:
    ts: datetime  # UTC
    running: bool
    power_level: int | None = None
    pump_hz: float | None = None


@dataclass(frozen=True)
class FillCandidate:
    """A detected fuel-level jump across a park, awaiting Dan's confirmation."""

    fill_id: str
    ts: datetime  # UTC
    est_gallons: float
    odometer_mi: float | None
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class FillUp:
    fill_id: str
    ts: datetime  # UTC
    source: FillSource
    fill_type: FillType
    status: FillStatus
    gallons: float | None
    price_per_gal: float | None = None
    odometer_mi: float | None = None
    lat: float | None = None
    lon: float | None = None


@dataclass(frozen=True)
class FuelState:
    """Persisted fuel-model state; restored from bigair's fsync'd state file (CLAUDE.md)."""

    ts: datetime  # UTC
    gal_after_last_fill: float
    engine_used_gal: float
    heater_used_gal: float
    last_fill_type: FillType
    stale: bool = False
    gap_gal: float = 0.0  # share of engine_used_gal estimated across data gaps
    miles_since_full: float | None = 0.0  # None: no known full fill to count from
    pump_gal_since_full: float = 0.0  # partial-fill pump gallons since the last full fill
    mpg_since_full: float | None = None  # true mpg of the latest full-to-full interval
    last_fill_id: str | None = None  # makes re-delivered fills a no-op
