"""Edge: Google Routes API and Places API (New) over HTTPS (#71).

Two calls, both POST with the key and a field mask in headers:
- ``computeRoutes``: origin, destination, optional intermediates → one route.
  Also used duration-only for POI detour times (#88), the Essentials SKU.
- ``places:searchText`` with ``searchAlongRouteParameters``: diesel stations
  along one stretch, with ``fuelOptions`` and ``routingSummaries``. This is the
  Text Search Enterprise + Atmosphere SKU, so a long trip costs tens of requests.

The key is never logged. Errors carry Google's error body, which names the
problem (API not enabled, bad field mask, quota).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from bigair.destinations import Destination
    from bigair.fuel_sources import Segment

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTE_FIELDS = "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline"
PLACE_FIELDS = (
    "places.id,places.displayName,places.location,places.formattedAddress,"
    "places.fuelOptions,routingSummaries,nextPageToken"
)
TIMEOUT_S = 20.0
M_PER_MI = 1609.344


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


_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def waypoint(place: str) -> dict[str, Any]:
    """An address, or ``"lat,lon"`` (e.g. the van's GPS position) as a location."""
    m = _LATLON.match(place)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}
    return {"address": place}


def compute_route(
    key: str,
    origin: str,
    destination: str,
    via: list[str],
    avoid_highways: bool = False,
    alternatives: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "origin": waypoint(origin),
        "destination": waypoint(destination),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }
    if via:
        body["intermediates"] = [waypoint(v) for v in via]
    if avoid_highways:  # slow mode (#90): Google's nearest thing to "no interstates"
        body["routeModifiers"] = {"avoidHighways": True}
    if alternatives and not via:  # #100; Google returns no alternatives with intermediates
        body["computeAlternativeRoutes"] = True
    return _post(ROUTES_URL, key, ROUTE_FIELDS, body)


def drive_leg(
    key: str, origin: tuple[float, float], dest: tuple[float, float]
) -> tuple[float, float]:
    """(seconds, road miles) between two points, for a POI detour (#88, #90)."""
    body: dict[str, Any] = {
        "origin": waypoint(f"{origin[0]},{origin[1]}"),
        "destination": waypoint(f"{dest[0]},{dest[1]}"),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }
    fields = "routes.duration,routes.distanceMeters"
    routes = _post(ROUTES_URL, key, fields, body).get("routes") or []
    if not routes:
        raise ValueError("no route to the POI")
    r = routes[0]
    return float(str(r.get("duration", "0s")).rstrip("s")), float(r["distanceMeters"]) / M_PER_MI


CAMP_FIELDS = "places.displayName,places.location,places.formattedAddress"


def search_campgrounds(key: str, bbox: tuple[float, float, float, float]) -> list[Destination]:
    """Campgrounds in a (south, west, north, east) box: discovery's fallback (#91)."""
    from bigair.destinations import Destination

    s, w, n, e = bbox
    body: dict[str, Any] = {
        "textQuery": "campground",
        "includedType": "campground",
        "pageSize": 20,
        "locationRestriction": {
            "rectangle": {
                "low": {"latitude": s, "longitude": w},
                "high": {"latitude": n, "longitude": e},
            }
        },
    }
    out: list[Destination] = []
    for p in _post(PLACES_URL, key, CAMP_FIELDS, body).get("places") or []:
        loc = p.get("location") or {}
        if "latitude" not in loc or "longitude" not in loc:
            continue
        name = (p.get("displayName") or {}).get("text") or "Campground"
        out.append(
            Destination(
                name,
                float(loc["latitude"]),
                float(loc["longitude"]),
                "campground",
                None,
                p.get("formattedAddress"),
            )  # fmt: skip
        )
    return out


def search_along(
    key: str,
    seg: Segment,
    page_token: str | None,
    query: str = "diesel",
    included_type: str | None = "gas_station",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "textQuery": query,
        "pageSize": 20,
        "searchAlongRouteParameters": {"polyline": {"encodedPolyline": seg.encoded}},
        "routingParameters": {
            "origin": {"latitude": seg.origin[0], "longitude": seg.origin[1]},
            "travelMode": "DRIVE",
        },
    }
    if included_type:
        body["includedType"] = included_type
    if page_token:
        body["pageToken"] = page_token
    return _post(PLACES_URL, key, PLACE_FIELDS, body)
