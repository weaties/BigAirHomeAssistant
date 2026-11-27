"""Edge: weather along the route (#75), over HTTPS.

- Open-Meteo ``/v1/forecast``: free, no key. Several locations go in one
  request as comma-separated latitude/longitude lists, and the response is a
  list in the same order. Times are requested as UTC (``timezone=GMT``).
- NWS ``/alerts/active?point=lat,lon``: US only, no key. NWS asks for a
  User-Agent; a generic one is sent, never personal contact details.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

METEO_URL = "https://api.open-meteo.com/v1/forecast"
NWS_URL = "https://api.weather.gov/alerts/active"
HOURLY = (
    "temperature_2m,precipitation_probability,precipitation,snowfall,"
    "weather_code,wind_speed_10m,wind_gusts_10m"
)
CHUNK = 50
USER_AGENT = "van-telemetry-client/1.0"
TIMEOUT_S = 20.0


def meteo_url(points: list[tuple[float, float]], days: int = 16) -> str:
    """16 days is Open-Meteo's longest; the far days are less certain."""
    q = {
        "latitude": ",".join(f"{p[0]:.4f}" for p in points),
        "longitude": ",".join(f"{p[1]:.4f}" for p in points),
        "hourly": HOURLY,
        "wind_speed_unit": "mph",
        "timezone": "GMT",
        "forecast_days": str(days),
    }
    return f"{METEO_URL}?{urlencode(q)}"


def fetch_forecasts(points: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """Hourly forecasts for every point, in order (chunked to keep URLs short)."""
    out: list[dict[str, Any]] = []
    for i in range(0, len(points), CHUNK):
        chunk = points[i : i + CHUNK]
        with urlopen(meteo_url(chunk), timeout=TIMEOUT_S) as resp:  # noqa: S310 — fixed https
            body = json.loads(resp.read())
        out.extend(body if isinstance(body, list) else [body])
    return out


def fetch_alerts(lat: float, lon: float) -> dict[str, Any]:
    req = Request(  # noqa: S310 — fixed https URL
        f"{NWS_URL}?{urlencode({'point': f'{lat:.4f},{lon:.4f}'})}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
    )
    with urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
        data: dict[str, Any] = json.loads(resp.read())
        return data
