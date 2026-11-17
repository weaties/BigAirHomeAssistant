"""Generate drive.csv — a synthetic 1 Hz drive with one full fill-up.

Deterministic: no randomness, no dependency on bigair. Every row's values
apply over [t, t + 1 s). Edit the SEGMENTS table and re-run; never hand-edit
drive.csv (tests/test_fixture_drive.py regenerates and compares).

    uv run python tests/fixtures/drive_synthetic/generate.py

Expected results live in expected.json and are derived BY HAND from the
segment table below, not from this script's output.

Segments (gph = gallons per hour of engine fuel):

    #   kind    seconds  mph   gph    miles   gallons
    1   idle      300     0    0.60    0.00    0.050
    2   city     1800    30    2.40   15.00    1.200
    3   highway  3600    65    4.20   65.00    4.200
    4   idle      570     0    0.60    0.00    0.095
    5   rural    1800    50    3.00   25.00    1.500
    6   idle       30     0    0.60    0.00    0.005   (pulled up at the pump)
    7   parked   1200     —    —       —       —       ignition off; full fill of 7.05 gal
    8   idle       60     0    0.60    0.00    0.010   ignition on, level settles
    9   drive     900    40    2.76   10.00    0.690

Pre-fill: 105 mi on 7.05 gal. Post-fill: 10 mi on 0.70 gal.
Tank starts at 47.0 gal (previous fill was full), odometer 41 200.0 mi.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

CAPACITY_GAL = 47.0
START = datetime(2026, 6, 1, 15, 0, 0, tzinfo=UTC)
START_ODOMETER_MI = 41_200.0
START_LAT = 39.500000  # arbitrary empty-desert origin, not a real route
START_LON = -111.000000
MI_PER_DEG_LAT = 69.172
LEVEL_STEP_PCT = 0.5  # synthetic sender resolution
FILL_GAL = 7.05

COLUMNS = [
    "ts",
    "ignition",
    "speed_mph",
    "fuel_rate_gph",
    "odometer_mi",
    "fuel_level_pct",
    "lat",
    "lon",
]


@dataclass(frozen=True)
class Segment:
    kind: str
    seconds: int
    mph: float
    gph: float
    ignition: bool = True


SEGMENTS = [
    Segment("idle", 300, 0.0, 0.60),
    Segment("city", 1800, 30.0, 2.40),
    Segment("highway", 3600, 65.0, 4.20),
    Segment("idle", 570, 0.0, 0.60),
    Segment("rural", 1800, 50.0, 3.00),
    Segment("idle", 30, 0.0, 0.60),
    Segment("parked", 1200, 0.0, 0.0, ignition=False),
    Segment("idle", 60, 0.0, 0.60),
    Segment("drive", 900, 40.0, 2.76),
]
FILL_AFTER_SEGMENT = 6  # 0-based index of the parked segment: fill happens there


def _level_pct(tank_gal: float) -> float:
    pct = tank_gal / CAPACITY_GAL * 100.0
    return round(round(pct / LEVEL_STEP_PCT) * LEVEL_STEP_PCT, 1)


def render() -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)

    t = START
    tank = CAPACITY_GAL
    odometer = START_ODOMETER_MI
    lat, lon = START_LAT, START_LON
    mi_per_deg_lon = MI_PER_DEG_LAT * math.cos(math.radians(START_LAT))

    for index, seg in enumerate(SEGMENTS):
        if index == FILL_AFTER_SEGMENT:
            tank_after_fill = tank + FILL_GAL
        for _ in range(seg.seconds):
            if seg.ignition:
                row = [
                    t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "1",
                    f"{seg.mph:.1f}",
                    f"{seg.gph:.2f}",
                    f"{odometer:.1f}",
                    f"{_level_pct(tank):.1f}",
                ]
            else:  # WiCAN/ECU asleep: no OBD values, GPS still logging
                row = [t.strftime("%Y-%m-%dT%H:%M:%SZ"), "0", "", "", "", ""]
            row += [f"{lat:.6f}", f"{lon:.6f}"]
            writer.writerow(row)

            miles = seg.mph / 3600.0
            odometer += miles
            lon += miles / mi_per_deg_lon  # due east
            tank -= seg.gph / 3600.0
            t += timedelta(seconds=1)
        if index == FILL_AFTER_SEGMENT:
            tank = tank_after_fill
    return out.getvalue()


def main() -> None:
    Path(__file__).with_name("drive.csv").write_text(render())


if __name__ == "__main__":
    main()
