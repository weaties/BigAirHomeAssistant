"""Weather along the route (#75): pure logic.

Points are sampled every ``every_mi`` along the route, each with its time
offset from the start of its leg (uniform speed over the routed duration).
Open-Meteo hourly series are fetched once per point (`weather_io.py`), so the
browser can re-time the weather for any departure within the 7-day window
without fetching again. NWS active alerts are listed separately.

Classification, for colouring the route:
- ``severe``: thunderstorm, freezing rain or drizzle, or gusts ≥ 45 mph
- ``snow``: snowfall, a snow weather code, or rain at or below 1 °C (ice risk)
- ``wet``: precipitation ≥ 0.3 mm/h, a ≥ 60 % chance, or a rain code
- ``clear``: anything else

The same thresholds are mirrored in ``planner_page.html`` for the departure
slider; ``CLASSIFY`` is sent to the page so the two can't drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bigair.fuel_sources import RouteInfo

CLASSIFY = {
    "severe_gust_mph": 45.0,
    "severe_codes": [56, 57, 66, 67, 95, 96, 99],
    "snow_codes": [71, 73, 75, 77, 85, 86],
    "ice_temp_c": 1.0,
    "wet_precip_mm": 0.3,
    "wet_prob_pct": 60.0,
    "wet_codes": [51, 53, 55, 61, 63, 65, 80, 81, 82],
}

_WMO = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm, hail",
    99: "Thunderstorm, hail",
}
WMO_LABELS = {str(k): v for k, v in _WMO.items()}


def wmo_label(code: int) -> str:
    return _WMO.get(code, "Unknown")


@dataclass(frozen=True)
class RoutePoint:
    mile: float
    """Position along the whole trip (the return leg continues the numbering)."""
    lat: float
    lon: float
    offset_s: float
    """Seconds after the start of this point's leg."""
    leg: str
    """``out`` or ``back``."""
    series: int
    """Index into the hourly forecasts (return points reuse the outbound ones)."""


def sample_points(route: RouteInfo, every_mi: float = 25.0) -> list[RoutePoint]:
    """Points every ``every_mi`` along the route, always including both ends."""
    targets: list[float] = []
    m = 0.0
    while m < route.total_mi - every_mi / 4:
        targets.append(m)
        m += every_mi
    targets.append(route.total_mi)
    out: list[RoutePoint] = []
    j = 0
    for t in targets:
        while j < len(route.cum_mi) - 2 and route.cum_mi[j + 1] < t:
            j += 1
        a, b = route.points[j], route.points[j + 1]
        span = route.cum_mi[j + 1] - route.cum_mi[j]
        f = 0.0 if span <= 0 else max(0.0, min(1.0, (t - route.cum_mi[j]) / span))
        lat = a[0] + f * (b[0] - a[0])
        lon = a[1] + f * (b[1] - a[1])
        offset = route.duration_s * t / route.total_mi if route.total_mi else 0.0
        out.append(RoutePoint(t, lat, lon, offset, "out", len(out)))
    return out


def return_leg_points(
    out: list[RoutePoint], out_mi: float, local_mi: float, out_s: float
) -> list[RoutePoint]:
    """The drive home over the same road: same places, reversed, timed from the return start."""
    return [
        RoutePoint(
            out_mi + local_mi + (out_mi - p.mile),
            p.lat,
            p.lon,
            out_s - p.offset_s,
            "back",
            p.series,
        )
        for p in reversed(out)
    ]


@dataclass(frozen=True)
class Hourly:
    times: tuple[datetime, ...]
    temp_c: tuple[float, ...]
    precip_prob_pct: tuple[float, ...]
    precip_mm: tuple[float, ...]
    snow_cm: tuple[float, ...]
    wind_mph: tuple[float, ...]
    gust_mph: tuple[float, ...]
    code: tuple[int, ...]
    elevation_m: float | None = None


def _nums(h: dict[str, Any], key: str, n: int) -> tuple[float, ...]:
    vals = h.get(key) or [None] * n
    return tuple(float(v) if v is not None else 0.0 for v in vals)


def decode_open_meteo(body: dict[str, Any] | list[dict[str, Any]]) -> list[Hourly]:
    """Open-Meteo forecast (one location → object, several → list) → hourly series.

    Requested with ``timezone=GMT``, so the ``time`` strings are UTC.
    """
    items = body if isinstance(body, list) else [body]
    out: list[Hourly] = []
    for item in items:
        h = item.get("hourly") or {}
        times = tuple(datetime.fromisoformat(t).replace(tzinfo=UTC) for t in h.get("time", []))
        n = len(times)
        out.append(
            Hourly(
                times=times,
                temp_c=_nums(h, "temperature_2m", n),
                precip_prob_pct=_nums(h, "precipitation_probability", n),
                precip_mm=_nums(h, "precipitation", n),
                snow_cm=_nums(h, "snowfall", n),
                wind_mph=_nums(h, "wind_speed_10m", n),
                gust_mph=_nums(h, "wind_gusts_10m", n),
                code=tuple(int(v) for v in _nums(h, "weather_code", n)),
                elevation_m=item.get("elevation"),
            )
        )
    return out


@dataclass(frozen=True)
class Conditions:
    temp_c: float
    precip_prob_pct: float
    precip_mm: float
    snow_cm: float
    wind_mph: float
    gust_mph: float
    code: int


def conditions_at(h: Hourly, when: datetime) -> Conditions | None:
    """Forecast for the hour nearest ``when``; None outside the forecast window."""
    if not h.times:
        return None
    step = 3600.0
    i = round((when - h.times[0]).total_seconds() / step)
    if i < 0 or i >= len(h.times):
        return None
    return Conditions(
        h.temp_c[i],
        h.precip_prob_pct[i],
        h.precip_mm[i],
        h.snow_cm[i],
        h.wind_mph[i],
        h.gust_mph[i],
        h.code[i],
    )


def classify(c: Conditions) -> str:
    k = CLASSIFY
    if c.code in k["severe_codes"] or c.gust_mph >= k["severe_gust_mph"]:  # type: ignore[operator]
        return "severe"
    wet = (
        c.precip_mm >= k["wet_precip_mm"]  # type: ignore[operator]
        or c.precip_prob_pct >= k["wet_prob_pct"]  # type: ignore[operator]
        or c.code in k["wet_codes"]  # type: ignore[operator]
    )
    if c.snow_cm > 0 or c.code in k["snow_codes"]:  # type: ignore[operator]
        return "snow"
    if wet and c.temp_c <= k["ice_temp_c"]:  # type: ignore[operator]
        return "snow"
    return "wet" if wet else "clear"


@dataclass(frozen=True)
class Alert:
    id: str
    event: str
    severity: str
    headline: str
    area: str
    onset: datetime | None
    ends: datetime | None


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(UTC) if s else None


def decode_nws_alerts(bodies: list[dict[str, Any]]) -> list[Alert]:
    """NWS ``/alerts/active?point=`` responses (one per point) → unique alerts."""
    seen: dict[str, Alert] = {}
    same: set[tuple[str, str]] = set()  # NWS reissues an alert under a new id
    for body in bodies:
        for f in body.get("features") or []:
            p = f.get("properties") or {}
            aid = str(p.get("id", ""))
            key = (str(p.get("event", "")), str(p.get("areaDesc", "")))
            if not aid or aid in seen or key in same:
                continue
            same.add(key)
            seen[aid] = Alert(
                id=aid,
                event=str(p.get("event", "")),
                severity=str(p.get("severity", "")),
                headline=str(p.get("headline") or p.get("event", "")),
                area=str(p.get("areaDesc", "")),
                onset=_ts(p.get("onset") or p.get("effective")),
                ends=_ts(p.get("ends") or p.get("expires")),
            )
    return list(seen.values())
