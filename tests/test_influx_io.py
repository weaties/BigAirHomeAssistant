"""Line protocol and the InfluxDB writer edge (#6). Schema: CLAUDE.md → Data model."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bigair import influx_io
from bigair.influx_io import (
    InfluxWriter,
    Point,
    bucket_for,
    engine_point,
    fuel_point,
    to_line,
    track_point,
    vehicle_point,
)
from bigair.signals import GpsFix, ObdSample

if TYPE_CHECKING:
    from urllib.request import Request

TS = datetime(2026, 6, 1, 15, 0, 0, 250_000, tzinfo=UTC)
TS_MS = 1_780_326_000_250


def test_basic_line() -> None:
    p = Point("fuel", TS, tags={"method": "fuel_rate"}, fields={"tank_gal": 39.95})
    assert to_line(p) == f"fuel,method=fuel_rate tank_gal=39.95 {TS_MS}"


def test_tags_are_sorted_and_fields_keep_order() -> None:
    p = Point("x", TS, tags={"b": "2", "a": "1"}, fields={"z_v": 1.0, "a_v": 2.0})
    assert to_line(p) == f"x,a=1,b=2 z_v=1.0,a_v=2.0 {TS_MS}"


def test_field_types() -> None:
    p = Point(
        "t",
        TS,
        fields={"f_mpg": 14.894, "n": 3, "on": True, "off": False, "trip_id": 'a "b"\\'},
    )
    assert to_line(p) == f't f_mpg=14.894,n=3i,on=true,off=false,trip_id="a \\"b\\"\\\\" {TS_MS}'


def test_floats_keep_full_precision_and_never_use_exponents() -> None:
    p = Point("t", TS, fields={"a": 1 / 3, "b": 1e-7, "c": 1e20, "d": -0.0})
    line = to_line(p)
    assert "a=0.3333333333333333" in line
    assert "e" not in line.split(" ")[1].replace("true", "")  # no exponent notation
    assert "b=0.0000001" in line
    assert "c=100000000000000000000.0" in line


def test_none_fields_are_dropped() -> None:
    p = Point("t", TS, fields={"a": 1.0, "b": None})
    assert to_line(p) == f"t a=1.0 {TS_MS}"


def test_point_with_no_fields_is_rejected() -> None:
    with pytest.raises(ValueError, match="no fields"):
        to_line(Point("t", TS, fields={"a": None}))


def test_nan_and_inf_are_dropped() -> None:
    p = Point("t", TS, fields={"a": float("nan"), "b": float("inf"), "c": 2.0})
    assert to_line(p) == f"t c=2.0 {TS_MS}"


def test_escaping() -> None:
    p = Point("m e,a", TS, tags={"k ,=": "v ,="}, fields={"f ,=": 1.0})
    assert to_line(p) == f"m\\ e\\,a,k\\ \\,\\==v\\ \\,\\= f\\ \\,\\==1.0 {TS_MS}"


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match="UTC"):
        to_line(Point("t", datetime(2026, 6, 1), fields={"a": 1.0}))  # noqa: DTZ001


def test_buckets() -> None:
    assert bucket_for("fuel") == "van_raw"
    assert bucket_for("engine") == "van_raw"
    for m in ("track", "fill_up", "trip"):
        assert bucket_for(m) == "van_history"


def test_track_point() -> None:
    fix = GpsFix(ts=TS, lat=39.5, lon=-111.0, speed_mph=60.0, alt_m=1500.0, hdop=0.9)
    assert to_line(track_point(fix, mpg_30s=15.5)) == (
        f"track,source=gps_bt lat=39.5,lon=-111.0,alt_m=1500.0,speed_mph=60.0,"
        f"hdop=0.9,mpg_30s=15.5 {TS_MS}"
    )


def test_vehicle_point_skips_missing_values() -> None:
    s = ObdSample(ts=TS, ignition=True, speed_mph=55.0, fuel_rate_gph=None, odometer_mi=None)
    assert to_line(vehicle_point(s)) == f"vehicle,source=wican speed_mph=55.0 {TS_MS}"


def test_engine_point_has_the_temperatures() -> None:
    s = ObdSample(
        ts=TS,
        ignition=True,
        speed_mph=0.0,
        fuel_rate_gph=None,
        coolant_temp_c=54.0,
        oil_temp_c=50.0,
        ambient_temp_c=6.0,
    )
    p = engine_point(s)
    assert p is not None
    assert to_line(p) == f"engine,source=wican coolant_temp_c=54.0,oil_temp_c=50.0 {TS_MS}"
    assert "ambient_temp_c=6.0" in to_line(vehicle_point(s))


def test_no_temperatures_means_no_engine_point() -> None:
    s = ObdSample(ts=TS, ignition=True, speed_mph=0.0, fuel_rate_gph=None)
    assert engine_point(s) is None


def test_fuel_point() -> None:
    p = fuel_point(
        TS,
        method="fuel_rate",
        tank_gal=39.95,
        engine_used_gal=7.05,
        heater_used_gal=0.0,
        range_mi=595.0,
        mpg_rolling=14.894,
        mpg_instant_30s=None,
        fuel_rate_gph=4.2,
    )
    assert to_line(p) == (
        f"fuel,method=fuel_rate tank_gal=39.95,engine_used_gal=7.05,heater_used_gal=0.0,"
        f"range_mi=595.0,mpg_rolling=14.894,fuel_rate_gph=4.2 {TS_MS}"
    )


def test_fuel_point_carries_the_stale_and_calibrating_flags() -> None:
    """Grafana's live panels hide a stale tank and label a calibrating one (#65)."""
    p = fuel_point(
        TS,
        "fuel_rate",
        39.95,
        None,
        None,
        None,
        None,
        None,
        None,
        stale=True,
        calibrating=False,
    )
    assert (
        to_line(p) == f"fuel,method=fuel_rate tank_gal=39.95,stale=true,calibrating=false {TS_MS}"
    )


def _schema() -> dict[str, set[str]]:
    """Measurement → field names, parsed from the CLAUDE.md Data model table."""
    text = (Path(__file__).parents[1] / "CLAUDE.md").read_text()
    schema: dict[str, set[str]] = {}
    for row in re.findall(r"^\| `(\w+)` \| \w+ \|[^|]*\|([^|]*)\|$", text, re.MULTILINE):
        schema[row[0]] = set(re.findall(r"`(\w+)`", row[1]))
    return schema


def test_mappers_only_write_fields_in_the_data_model() -> None:
    schema = _schema()
    fix = GpsFix(ts=TS, lat=1.0, lon=2.0, speed_mph=1.0, alt_m=1.0, heading_deg=1.0, hdop=1.0)
    sample = ObdSample(
        ts=TS,
        ignition=True,
        speed_mph=1.0,
        fuel_rate_gph=1.0,
        odometer_mi=1.0,
        coolant_temp_c=1.0,
        oil_temp_c=1.0,
        ambient_temp_c=1.0,
    )
    engine = engine_point(sample)
    assert engine is not None
    points = [
        engine,
        track_point(fix, mpg_30s=1.0, trip_id="t1", grade_pct=1.0),
        vehicle_point(sample),
        fuel_point(
            TS, "fuel_rate", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, stale=False, calibrating=True
        ),
    ]
    for p in points:
        assert set(p.fields) <= schema[p.measurement], p.measurement


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _writer() -> InfluxWriter:
    return InfluxWriter("http://influx:8086", org="bigair", token="t0k", max_buffer=3)


def test_writer_posts_line_protocol_per_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Request] = []

    def fake_urlopen(req: Request, timeout: float) -> FakeResponse:
        sent.append(req)
        return FakeResponse(204)

    monkeypatch.setattr(influx_io, "urlopen", fake_urlopen)
    w = _writer()
    w.add(Point("fuel", TS, fields={"tank_gal": 1.0}))
    w.add(Point("track", TS, fields={"lat": 1.0}))
    assert w.flush() == 2
    urls = sorted(r.full_url for r in sent)
    assert urls == [
        "http://influx:8086/api/v2/write?org=bigair&bucket=van_history&precision=ms",
        "http://influx:8086/api/v2/write?org=bigair&bucket=van_raw&precision=ms",
    ]
    assert all(r.get_header("Authorization") == "Token t0k" for r in sent)
    assert w.pending == 0


def test_writer_keeps_points_when_influx_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(req: Request, timeout: float) -> FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr(influx_io, "urlopen", down)
    w = _writer()
    w.add(Point("fuel", TS, fields={"tank_gal": 1.0}))
    assert w.flush() == 0
    assert w.pending == 1


def test_writer_buffer_is_bounded_and_drops_oldest() -> None:
    w = _writer()
    for i in range(5):
        w.add(Point("fuel", TS, fields={"n": i}))
    assert w.pending == 3
    assert w.dropped == 2


def test_writer_refuses_while_the_clock_is_unsynced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision #24: never write wrong timestamps after a cold boot."""
    calls: list[Any] = []
    monkeypatch.setattr(influx_io, "urlopen", lambda req, timeout: calls.append(req))
    w = InfluxWriter("http://influx:8086", org="bigair", token="t", clock_ok=lambda: False)
    w.add(Point("fuel", TS, fields={"tank_gal": 1.0}))
    assert w.flush() == 0
    assert calls == []
    assert w.pending == 1
