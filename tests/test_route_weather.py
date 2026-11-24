"""Weather along the route (#75): sampling, Open-Meteo/NWS decoding, conditions at ETA."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.fuel_sources import RouteInfo
from bigair.route_weather import (
    Conditions,
    classify,
    conditions_at,
    decode_nws_alerts,
    decode_open_meteo,
    return_leg_points,
    sample_points,
    wmo_label,
)

T0 = datetime(2026, 11, 28, 16, 0, tzinfo=UTC)


def _route() -> RouteInfo:
    pts = [(40.0 + i * 0.1, -120.0) for i in range(21)]
    return RouteInfo.from_points(pts, road_mi=100.0, duration_s=7200.0)


def test_sample_points_every_n_miles_with_both_ends() -> None:
    pts = sample_points(_route(), every_mi=25.0)
    assert [round(p.mile) for p in pts] == [0, 25, 50, 75, 100]
    assert pts[0].lat == pytest.approx(40.0)
    assert pts[-1].lat == pytest.approx(42.0)
    assert pts[2].lat == pytest.approx(41.0, abs=0.01)
    assert pts[2].offset_s == pytest.approx(3600.0)  # halfway at uniform speed
    assert all(p.leg == "out" for p in pts)


def test_return_leg_reuses_points_reversed() -> None:
    out = sample_points(_route(), every_mi=50.0)
    back = return_leg_points(out, out_mi=100.0, local_mi=10.0, out_s=7200.0)
    assert [p.leg for p in back] == ["back"] * 3
    assert [round(p.mile) for p in back] == [110, 160, 210]
    assert [p.lat for p in back] == [p.lat for p in reversed(out)]
    assert [p.offset_s for p in back] == pytest.approx([0.0, 3600.0, 7200.0])
    assert [p.series for p in back] == [p.series for p in reversed(out)]


def _meteo(n_hours: int = 48, **over: list[float]) -> dict[str, object]:
    times = [(T0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(n_hours)]
    hourly: dict[str, object] = {
        "time": times,
        "temperature_2m": [5.0] * n_hours,
        "precipitation_probability": [0] * n_hours,
        "precipitation": [0.0] * n_hours,
        "snowfall": [0.0] * n_hours,
        "weather_code": [0] * n_hours,
        "wind_speed_10m": [5.0] * n_hours,
        "wind_gusts_10m": [10.0] * n_hours,
    }
    hourly.update(over)
    return {"latitude": 40.0, "longitude": -120.0, "elevation": 1300.0, "hourly": hourly}


def test_decode_open_meteo_single_and_list() -> None:
    one = decode_open_meteo(_meteo())
    assert len(one) == 1
    assert one[0].times[0] == T0
    assert one[0].elevation_m == 1300.0
    two = decode_open_meteo([_meteo(), _meteo()])
    assert len(two) == 2


def test_conditions_at_picks_nearest_hour() -> None:
    temps = [float(h) for h in range(48)]
    (h,) = decode_open_meteo(_meteo(temperature_2m=temps))
    c = conditions_at(h, T0 + timedelta(hours=5, minutes=20))
    assert c is not None
    assert c.temp_c == 5.0
    assert conditions_at(h, T0 + timedelta(hours=5, minutes=40)).temp_c == 6.0  # type: ignore[union-attr]


def test_conditions_outside_forecast_window_are_unknown() -> None:
    (h,) = decode_open_meteo(_meteo(n_hours=24))
    assert conditions_at(h, T0 + timedelta(hours=30)) is None
    assert conditions_at(h, T0 - timedelta(hours=3)) is None


def _c(**kw: float) -> Conditions:
    base = {
        "temp_c": 10.0,
        "precip_prob_pct": 0.0,
        "precip_mm": 0.0,
        "snow_cm": 0.0,
        "wind_mph": 5.0,
        "gust_mph": 10.0,
        "code": 0,
    }
    base.update(kw)
    return Conditions(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "cls"),
    [
        ({}, "clear"),
        ({"precip_prob_pct": 70.0}, "wet"),
        ({"precip_mm": 1.0, "code": 61}, "wet"),
        ({"snow_cm": 0.5, "code": 73}, "snow"),
        ({"code": 71}, "snow"),
        ({"precip_mm": 1.0, "temp_c": 0.0, "code": 61}, "snow"),  # rain at freezing = ice risk
        ({"code": 95}, "severe"),
        ({"gust_mph": 50.0}, "severe"),
        ({"code": 67, "precip_mm": 1.0}, "severe"),  # freezing rain
    ],
)
def test_classify(kw: dict[str, float], cls: str) -> None:
    assert classify(_c(**kw)) == cls


def test_wmo_labels() -> None:
    assert wmo_label(0) == "Clear"
    assert wmo_label(73) == "Snow"
    assert wmo_label(95) == "Thunderstorm"
    assert wmo_label(1234) == "Unknown"


def test_decode_nws_alerts_dedupes() -> None:
    feature = {
        "properties": {
            "id": "urn:a1",
            "event": "Winter Storm Warning",
            "severity": "Severe",
            "headline": "Winter Storm Warning issued for Siskiyou",
            "areaDesc": "Siskiyou County",
            "onset": "2026-11-28T10:00:00-08:00",
            "ends": "2026-11-29T10:00:00-08:00",
        }
    }
    reissued = {"properties": {**feature["properties"], "id": "urn:a2"}}
    alerts = decode_nws_alerts([{"features": [feature]}, {"features": [feature, reissued]}, {}])
    assert len(alerts) == 1
    a = alerts[0]
    assert a.event == "Winter Storm Warning"
    assert a.onset == datetime(2026, 11, 28, 18, 0, tzinfo=UTC)
    assert a.area == "Siskiyou County"
