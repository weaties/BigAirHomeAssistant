"""Edge: EIA API v2 weekly retail diesel prices (#71).

``petroleum/pri/gnd``, product ``EPD2D`` (No 2 diesel), for the US, West Coast
except California (``R5XCA``) and California (``SCA``). Washington and Oregon
have no diesel series of their own. EIA publishes weekly (Mondays), so one
fetch per trip is plenty. ``DEMO_KEY`` works but is rate-limited; a free key
comes from eia.gov/opendata.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

EIA_URL = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
AREAS = ("NUS", "R5XCA", "SCA")
TIMEOUT_S = 20.0


def diesel_url(key: str, weeks: int = 5) -> str:
    params: list[tuple[str, str]] = [
        ("api_key", key),
        ("frequency", "weekly"),
        ("data[]", "value"),
        ("facets[product][]", "EPD2D"),
        *(("facets[duoarea][]", a) for a in AREAS),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("length", str(len(AREAS) * weeks)),
    ]
    return f"{EIA_URL}?{urlencode(params)}"


def fetch_diesel(key: str, weeks: int = 5) -> dict[str, Any]:
    with urlopen(diesel_url(key, weeks), timeout=TIMEOUT_S) as resp:  # noqa: S310 — fixed https
        data: dict[str, Any] = json.loads(resp.read())
        return data
