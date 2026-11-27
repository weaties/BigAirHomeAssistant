"""'Ask the van' (#94): read-only InfluxQL for Claude, and the briefing schedule.

Claude can query the van's history with InfluxQL through the planner's
read-only Grafana token. That token can't write, and every query is checked
here as well:

- exactly one ``SELECT`` or ``SHOW`` statement, with no ``INTO``;
- a ``SELECT`` needs a time range or a ``LIMIT`` (at most ``MAX_LIMIT``);
- results are trimmed to ``MAX_ROWS`` rows per series, with floats rounded.

``SCHEMA`` is the measurement list Claude works from. A test keeps it in step
with the Data model table in CLAUDE.md.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

MAX_ROWS = 200
MAX_LIMIT = 5000
BRIEFING_LATEST_H = 3
"""Send the evening briefing up to this many hours after its time, never later."""

DBS = ("van_raw", "van_history")

SCHEMA = """InfluxDB databases: van_raw (native-rate, 400 days) and van_history (1-minute \
aggregates, events, kept forever). Times are UTC. Every field name ends in its unit.

van_raw:
- engine (tag source): rpm, coolant_temp_c, intake_air_temp_c, maf_g_s, faer_ratio, \
engine_load_pct, boost_kpa, fuel_rate_lph
- vehicle (tag source): speed_mph, odometer_mi, battery_v
- driver (tag source): throttle_pct, cruise_active, cruise_set_mph, brake_active
- fuel (tag method): tank_gal, engine_used_gal, heater_used_gal, range_mi, mpg_rolling, \
mpg_instant_30s, fuel_rate_gph, stale, calibrating
- heater (tag method): running, power_level, pump_hz, fuel_gph, used_gal
- electrical (tag device: bmv, mppt, dcdc; every 10 s): voltage_v, current_a, power_w, \
soc_pct, consumed_ah, time_to_go_s (bmv); pv_power_w, yield_today_wh, state (mppt); \
input_voltage_v, state, off_reason (dcdc)
- electrical device=house: loads_w, alternator_w, estimated (bigair-derived)
- load (tag circuit: fan, webasto, lights, internet, inverter, pi): current_a, voltage_v, power_w

van_history:
- track (tag source): lat, lon, alt_m, speed_mph, heading_deg, mpg_30s, hdop, trip_id
- fill_up (tags source, fill_type, status): gallons, price_per_gal, total_cost, \
odometer_mi, lat, lon, tank_before_gal, tank_after_gal, mpg_since_full, fill_id
- trip (no tags): distance_mi, fuel_gal, mpg, duration_s, idle_s, cruise_share, \
brake_events, trip_id
- energy (no tags; one point per local day at local midnight): solar_wh, alternator_wh, \
battery_in_wh, battery_out_wh, loads_wh, estimated_wh

Not every measurement has data yet (some sensors aren't fitted). SHOW FIELD KEYS FROM \
<measurement> or a short LIMIT query tells you what's there."""

_FORBIDDEN = re.compile(r"\b(INTO|DROP|DELETE|CREATE|ALTER|GRANT|REVOKE|KILL)\b", re.I)
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)", re.I)


def validate_influxql(q: str) -> str:
    """The query, trimmed, if it's a single bounded read; ValueError otherwise."""
    q = q.strip().rstrip(";").strip()
    if not q:
        raise ValueError("empty query")
    if ";" in q:
        raise ValueError("one statement at a time")
    head = q.split(None, 1)[0].upper()
    if head not in ("SELECT", "SHOW"):
        raise ValueError("only SELECT or SHOW queries")
    m = _FORBIDDEN.search(q)
    if m:
        raise ValueError(f"{m.group(1).upper()} isn't allowed")
    if head == "SELECT":
        lim = _LIMIT.search(q)
        if lim and int(lim.group(1)) > MAX_LIMIT:
            raise ValueError(f"LIMIT must be {MAX_LIMIT} or less")
        if not re.search(r"\btime\s*[<>]=?", q, re.I) and not lim:
            raise ValueError("a SELECT needs a time range or a LIMIT")
    return q


def _round(v: object) -> object:
    return round(v, 3) if isinstance(v, float) else v


def shape_result(body: dict[str, Any]) -> dict[str, Any]:
    """InfluxQL JSON → compact series for Claude, at most MAX_ROWS rows each."""
    results = body.get("results") or [{}]
    first = results[0]
    if first.get("error"):
        return {"error": str(first["error"])}
    out: list[dict[str, Any]] = []
    truncated = False
    for s in first.get("series") or []:
        values = s.get("values") or []
        truncated = truncated or len(values) > MAX_ROWS
        out.append(
            {
                "name": s.get("name"),
                "tags": s.get("tags"),
                "columns": s.get("columns"),
                "values": [[_round(v) for v in row] for row in values[:MAX_ROWS]],
            }
        )
    return {"series": out, "truncated": truncated}


def briefing_due(now_local: datetime, at: tuple[int, int], last_date: str | None) -> bool:
    """Time for tonight's briefing: after ``at`` local, within BRIEFING_LATEST_H, once a day."""
    today = now_local.date().isoformat()
    if last_date == today:
        return False
    start = now_local.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    hours = (now_local - start).total_seconds() / 3600
    return 0 <= hours < BRIEFING_LATEST_H
