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
