"""Shared fixtures. The synthetic drive is the fuel-model ground truth."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bigair.signals import GpsFix, ObdSample

DRIVE_DIR = Path(__file__).parent / "fixtures" / "drive_synthetic"


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _opt(value: str) -> float | None:
    return float(value) if value else None


@dataclass(frozen=True)
class Drive:
    obd: list[ObdSample]
    gps: list[GpsFix]
    expected: dict[str, Any]

    def until(self, ts: datetime) -> list[ObdSample]:
        """OBD samples strictly before ts."""
        return [s for s in self.obd if s.ts < ts]


@pytest.fixture(scope="session")
def drive() -> Drive:
    obd: list[ObdSample] = []
    gps: list[GpsFix] = []
    with (DRIVE_DIR / "drive.csv").open() as f:
        for row in csv.DictReader(f):
            ts = parse_ts(row["ts"])
            obd.append(
                ObdSample(
                    ts=ts,
                    ignition=row["ignition"] == "1",
                    speed_mph=_opt(row["speed_mph"]),
                    fuel_rate_gph=_opt(row["fuel_rate_gph"]),
                    odometer_mi=_opt(row["odometer_mi"]),
                    fuel_level_pct=_opt(row["fuel_level_pct"]),
                )
            )
            gps.append(GpsFix(ts=ts, lat=float(row["lat"]), lon=float(row["lon"])))
    expected = json.loads((DRIVE_DIR / "expected.json").read_text())
    return Drive(obd=obd, gps=gps, expected=expected)
