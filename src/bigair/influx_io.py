"""InfluxDB writer edge — bigair is the only writer (Decision #4).

Serialises points to line protocol per CLAUDE.md → Data model and POSTs them
to the InfluxDB 2.x write API (stdlib only). Points are buffered in memory
(bounded, oldest dropped) while Influx is down or the clock isn't synced
(Decision #24: never write wrong timestamps after a cold boot).
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from bigair.fuel_model import FillResult
    from bigair.signals import FillUp, GpsFix, ObdSample

FieldValue = float | int | bool | str | None

RAW_BUCKET = "van_raw"
HISTORY_BUCKET = "van_history"
HISTORY_MEASUREMENTS = frozenset({"track", "fill_up", "trip"})


@dataclass(frozen=True)
class Point:
    measurement: str
    ts: datetime  # UTC
    tags: dict[str, str] = field(default_factory=dict)
    fields: dict[str, FieldValue] = field(default_factory=dict)


def bucket_for(measurement: str) -> str:
    return HISTORY_BUCKET if measurement in HISTORY_MEASUREMENTS else RAW_BUCKET


def _escape(text: str, chars: str) -> str:
    for ch in chars:
        text = text.replace(ch, "\\" + ch)
    return text


def _key(text: str) -> str:
    return _escape(text, ", =")


def _float(value: float) -> str:
    if value == int(value) and abs(value) < 1e16:
        return f"{int(value)}.0"
    text = repr(value)
    if "e" in text or "E" in text:
        text = f"{value:.20f}".rstrip("0")
        if text.endswith("."):
            text += "0"
    return text


def _field(value: float | int | bool | str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return _float(value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_line(p: Point) -> str:
    """One line of InfluxDB line protocol, millisecond precision."""
    if p.ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware UTC")
    fields = [
        f"{_key(k)}={_field(v)}"
        for k, v in p.fields.items()
        if v is not None and not (isinstance(v, float) and not math.isfinite(v))
    ]
    if not fields:
        raise ValueError(f"{p.measurement}: no fields to write")
    head = _escape(p.measurement, ", ")
    tags = "".join(f",{_key(k)}={_key(v)}" for k, v in sorted(p.tags.items()))
    ms = round(p.ts.timestamp() * 1000)
    return f"{head}{tags} {','.join(fields)} {ms}"


# --- mappers: dataclass → Point (field names must be in the Data model table) ---


def track_point(
    fix: GpsFix,
    mpg_30s: float | None = None,
    trip_id: str | None = None,
    grade_pct: float | None = None,
) -> Point:
    return Point(
        "track",
        fix.ts,
        tags={"source": "gps_bt"},  # XGPS160 over Bluetooth (Decision #28)
        fields={
            "lat": fix.lat,
            "lon": fix.lon,
            "alt_m": fix.alt_m,
            "speed_mph": fix.speed_mph,
            "heading_deg": fix.heading_deg,
            "hdop": fix.hdop,
            "mpg_30s": mpg_30s,
            "grade_pct": None if grade_pct is None else round(grade_pct, 2),
            "trip_id": trip_id,
        },
    )


def vehicle_point(sample: ObdSample) -> Point:
    return Point(
        "vehicle",
        sample.ts,
        tags={"source": "wican"},
        fields={
            "speed_mph": sample.speed_mph,
            "odometer_mi": sample.odometer_mi,
            "battery_v": sample.battery_v,
        },
    )


def fuel_point(
    ts: datetime,
    method: str,
    tank_gal: float | None,
    engine_used_gal: float | None,
    heater_used_gal: float | None,
    range_mi: float | None,
    mpg_rolling: float | None,
    mpg_instant_30s: float | None,
    fuel_rate_gph: float | None,
    *,
    stale: bool | None = None,
    calibrating: bool | None = None,
) -> Point:
    return Point(
        "fuel",
        ts,
        tags={"method": method},
        fields={
            "tank_gal": tank_gal,
            "engine_used_gal": engine_used_gal,
            "heater_used_gal": heater_used_gal,
            "range_mi": range_mi,
            "mpg_rolling": mpg_rolling,
            "mpg_instant_30s": mpg_instant_30s,
            "fuel_rate_gph": fuel_rate_gph,
            # Lets Grafana hide a stale tank and label a calibrating one (#65).
            "stale": stale,
            "calibrating": calibrating,
        },
    )


def fill_up_point(fill: FillUp, result: FillResult | None) -> Point:
    """One fill-up in van_history; same fill_id + timestamp + tags overwrites (idempotent)."""
    total = (
        None
        if fill.gallons is None or fill.price_per_gal is None
        else round(fill.gallons * fill.price_per_gal, 2)
    )
    return Point(
        "fill_up",
        fill.ts,
        tags={
            "source": fill.source.value,
            "fill_type": fill.fill_type.value,
            "status": fill.status.value,
        },
        fields={
            "gallons": fill.gallons,
            "price_per_gal": fill.price_per_gal,
            "total_cost": total,
            "odometer_mi": fill.odometer_mi,
            "lat": fill.lat,
            "lon": fill.lon,
            "tank_before_gal": result.tank_before_gal if result else None,
            "tank_after_gal": result.tank_after_gal if result else None,
            "mpg_since_full": result.mpg_since_full if result else None,
            "fill_id": fill.fill_id,
        },
    )


# --- writer ---


class InfluxWriter:
    """Buffers lines and writes them per bucket. Synchronous; call flush() from a thread."""

    def __init__(
        self,
        url: str,
        org: str,
        token: str,
        max_buffer: int = 50_000,
        timeout_s: float = 10.0,
        clock_ok: Callable[[], bool] = lambda: True,
    ) -> None:
        self.url = url.rstrip("/")
        self.org = org
        self.token = token
        self.timeout_s = timeout_s
        self.clock_ok = clock_ok
        self._buffer: deque[tuple[str, str]] = deque(maxlen=max_buffer)
        self._lock = threading.Lock()  # add() on the event loop, flush() in a worker thread
        self.dropped = 0

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def add(self, point: Point) -> None:
        line = (bucket_for(point.measurement), to_line(point))
        with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                self.dropped += 1
            self._buffer.append(line)

    def add_all(self, points: Iterable[Point]) -> None:
        for p in points:
            self.add(p)

    def flush(self) -> int:
        """Write everything buffered; return lines written. Failures stay buffered."""
        if not self._buffer or not self.clock_ok():
            return 0
        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()
        by_bucket: dict[str, list[str]] = {}
        for bucket, line in batch:
            by_bucket.setdefault(bucket, []).append(line)
        written: set[str] = set()
        for bucket, lines in by_bucket.items():
            query = urlencode({"org": self.org, "bucket": bucket, "precision": "ms"})
            req = Request(  # noqa: S310 — URL comes from our own config
                f"{self.url}/api/v2/write?{query}",
                data="\n".join(lines).encode(),
                method="POST",
                headers={
                    "Authorization": f"Token {self.token}",
                    "Content-Type": "text/plain; charset=utf-8",
                },
            )
            try:
                with urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                    if resp.status != 204:
                        logger.warning("influx write to {} returned {}", bucket, resp.status)
                        continue
            except OSError as e:
                logger.warning("influx write to {} failed: {}", bucket, e)
                continue
            written.add(bucket)
        sent = [item for item in batch if item[0] in written]
        failed = [item for item in batch if item[0] not in written]
        with self._lock:
            # Put failures back in front of anything added meanwhile (oldest first).
            newer = list(self._buffer)
            self._buffer.clear()
            self._buffer.extend(failed + newer)
        return len(sent)
