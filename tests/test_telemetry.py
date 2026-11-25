"""'Ask the van' (#94): InfluxQL validation, result shaping, and the schema Claude sees."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from bigair.telemetry import (
    MAX_ROWS,
    SCHEMA,
    briefing_due,
    shape_result,
    validate_influxql,
)

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "q",
    [
        "SELECT mean(mpg_rolling) FROM fuel WHERE time > now() - 1d GROUP BY time(1h)",
        "select last(tank_gal) from fuel where time > now() - 2h",
        "SELECT * FROM heater WHERE time > now() - 7d LIMIT 50",
        "SHOW FIELD KEYS FROM engine",
        "SHOW MEASUREMENTS",
        "SELECT max(coolant_temp_c) FROM engine WHERE time >= '2026-11-24T00:00:00Z'",
    ],
)
def test_read_only_bounded_queries_pass(q: str) -> None:
    assert validate_influxql(q) == q.strip().rstrip(";")


@pytest.mark.parametrize(
    ("q", "why"),
    [
        ("SELECT * INTO copy FROM fuel WHERE time > now() - 1h", "INTO"),
        ("DROP MEASUREMENT fuel", "SELECT or SHOW"),
        ("DELETE FROM fuel", "SELECT or SHOW"),
        ("SELECT * FROM fuel WHERE time > now() - 1h; DROP SERIES FROM fuel", "one statement"),
        ("SELECT * FROM fuel", "time range or a LIMIT"),
        ("CREATE DATABASE x", "SELECT or SHOW"),
        ("", "empty"),
        ("SELECT * FROM fuel WHERE time > now() - 1h LIMIT 100000", "LIMIT"),
    ],
)
def test_anything_else_is_refused(q: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        validate_influxql(q)


def test_results_are_compact_and_capped() -> None:
    body = {
        "results": [
            {
                "series": [
                    {
                        "name": "fuel",
                        "columns": ["time", "mean"],
                        "values": [
                            [f"2026-11-24T{h % 24:02d}:00:00Z", 13.123456] for h in range(500)
                        ],
                    }
                ]
            }
        ]
    }
    out = shape_result(body)
    s = out["series"][0]
    assert s["name"] == "fuel" and s["columns"] == ["time", "mean"]
    assert len(s["values"]) == MAX_ROWS
    assert s["values"][0][1] == 13.123
    assert out["truncated"] is True


def test_influx_errors_come_back_as_errors() -> None:
    assert shape_result({"results": [{"error": "field not found"}]}) == {"error": "field not found"}


def test_schema_names_every_measurement_in_the_claude_md_table() -> None:
    table = (ROOT / "CLAUDE.md").read_text().split("## Data model")[1].split("## Testing")[0]
    names = set(re.findall(r"^\| `(\w+)` \|", table, re.M))
    assert names  # the table parsed
    for n in names:
        assert f"{n} (" in SCHEMA, f"telemetry SCHEMA is missing measurement {n}"


# --- the briefing schedule ---------------------------------------------------------

MDT = timezone(timedelta(hours=-6))


def test_briefing_is_due_once_a_day_after_its_time() -> None:
    at = (19, 0)
    before = datetime(2026, 11, 25, 18, 59, tzinfo=MDT)
    after = datetime(2026, 11, 25, 19, 1, tzinfo=MDT)
    assert briefing_due(before, at, last_date=None) is False
    assert briefing_due(after, at, last_date=None) is True
    assert briefing_due(after, at, last_date="2026-11-25") is False  # already sent today
    assert briefing_due(after, at, last_date="2026-11-24") is True


def test_briefing_is_not_sent_late_at_night_after_a_restart() -> None:
    # Pi rebooted at 02:00: yesterday's evening is gone; don't send a briefing at 2am.
    assert briefing_due(datetime(2026, 11, 26, 2, 0, tzinfo=MDT), (19, 0), "2026-11-24") is False
    assert briefing_due(datetime(2026, 11, 25, 23, 30, tzinfo=MDT), (19, 0), None) is False


def test_briefing_due_uses_local_time_not_utc() -> None:
    utc = datetime(2026, 11, 26, 0, 30, tzinfo=UTC)  # 18:30 MDT on the 25th
    assert briefing_due(utc.astimezone(MDT), (19, 0), None) is False
