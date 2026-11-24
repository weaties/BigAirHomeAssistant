"""Edge: Google Routes API and Places API (New) over HTTPS (#71).

Two calls, both POST with the key and a field mask in headers:
- ``computeRoutes``: origin, destination, optional intermediates → one route
- ``places:searchText`` with ``searchAlongRouteParameters``: diesel stations
  along one stretch, with ``fuelOptions`` and ``routingSummaries``. This is the
  Text Search Enterprise + Atmosphere SKU, so a long trip costs tens of requests.

The key is never logged. Errors carry Google's error body, which names the
problem (API not enabled, bad field mask, quota).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from bigair.fuel_sources import Segment

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTE_FIELDS = "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline"
PLACE_FIELDS = (
    "places.id,places.displayName,places.location,places.formattedAddress,"
    "places.fuelOptions,routingSummaries,nextPageToken"
)
TIMEOUT_S = 20.0


class GoogleApiError(RuntimeError):
    pass


def _post(url: str, key: str, fields: str, body: dict[str, Any]) -> dict[str, Any]:
    req = Request(  # noqa: S310 — fixed https URLs
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": fields,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
            data: dict[str, Any] = json.loads(resp.read())
            return data
    except HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise GoogleApiError(f"{url.split('/')[2]} HTTP {e.code}: {detail}") from e


def compute_route(key: str, origin: str, destination: str, via: list[str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }
    if via:
        body["intermediates"] = [{"address": v} for v in via]
    return _post(ROUTES_URL, key, ROUTE_FIELDS, body)


def search_along(key: str, seg: Segment, page_token: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "textQuery": "diesel",
        "includedType": "gas_station",
        "pageSize": 20,
        "searchAlongRouteParameters": {"polyline": {"encodedPolyline": seg.encoded}},
        "routingParameters": {
            "origin": {"latitude": seg.origin[0], "longitude": seg.origin[1]},
            "travelMode": "DRIVE",
        },
    }
    if page_token:
        body["pageToken"] = page_token
    return _post(PLACES_URL, key, PLACE_FIELDS, body)
