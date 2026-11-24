"""Live fuel-plan inputs (#71): route geometry, station prices, price trend.

Pure functions only. The HTTP edges (`google_io.py`, `eia_io.py`) fetch JSON,
and this module decodes it into ``fuel_plan.Station``s.

Sources (field names from the documentation, checked 2026-11-23):
- Routes API ``computeRoutes``: ``routes[].distanceMeters``, ``duration`` ("123s"),
  ``polyline.encodedPolyline``
- Places API (New) ``places:searchText`` with ``searchAlongRouteParameters``:
  ``places[]`` (``id``, ``displayName.text``, ``location``, ``formattedAddress``,
  ``fuelOptions.fuelPrices[]`` with ``type``, ``price`` Money, ``updateTime``) and
  ``routingSummaries[]``, index-aligned with ``places[]``. Each summary's
  ``legs`` are origin → place and place → polyline end. US prices are per
  gallon. Where a station posts several prices for one type, the API returns
  the standard (highest) one, so a pump price may be a little lower.
- EIA API v2 ``petroleum/pri/gnd``: weekly retail ``EPD2D`` (No 2 diesel) by
  ``duoarea``. Washington and Oregon have no diesel series of their own, so the
  West Coast uses ``R5XCA`` (PADD 5 except California) and California uses ``SCA``.

Detour is not documented. It is derived as (origin → place + place → end) −
(segment length). Without a routing summary it falls back to twice the
station's distance off the route.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bigair.fuel_plan import Station

if TYPE_CHECKING:
    from collections.abc import Callable

M_PER_MI = 1609.344
_EARTH_MI = 3958.7613
DETOUR_MPH = 30.0
"""Speed assumed for getting off the highway to the pump and back."""
DRIFT_CAP_PER_DAY = 0.02
"""Largest trend applied, in $/gal/day. Trends are a small term, not a forecast."""

# Diesel the van can use, in order of preference.
_DIESEL_KINDS = ("DIESEL", "TRUCK_DIESEL")
_STATE_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}")
_BRANDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("loves", re.compile(r"\blove'?s\b", re.IGNORECASE)),
    ("ta", re.compile(r"^TA\b|\btravel ?centers? of america\b", re.IGNORECASE)),
    ("petro", re.compile(r"\bpetro\b(?!l)", re.IGNORECASE)),
)
BRAND_LABEL = {"loves": "Love's", "ta": "TA", "petro": "Petro"}
OPEN_ROADS_DEFAULT = {"loves": 0.30, "ta": 0.30, "petro": 0.30}
"""Open Roads (TSD) member discount, $/gal: the low end of the reported 30–50¢.
The real member price varies by station and is shown in the TSD Open Roads app."""
_WEST_COAST_EX_CA = {"WA", "OR", "NV", "AZ", "AK", "HI"}


# --- polyline -----------------------------------------------------------------


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Google encoded polyline (precision 5) → [(lat, lon), ...]."""
    pts: list[tuple[float, float]] = []
    idx = lat = lon = 0
    while idx < len(encoded):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        pts.append((lat / 1e5, lon / 1e5))
    return pts


def encode_polyline(points: list[tuple[float, float]]) -> str:
    out: list[str] = []
    plat = plon = 0
    for lat, lon in points:
        ilat, ilon = round(lat * 1e5), round(lon * 1e5)
        for delta in (ilat - plat, ilon - plon):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def _haversine_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    )
    return 2 * _EARTH_MI * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class RouteInfo:
    points: tuple[tuple[float, float], ...]
    cum_mi: tuple[float, ...]
    """Road miles from the start to each point (scaled to the routed distance)."""
    duration_s: float

    @property
    def total_mi(self) -> float:
        return self.cum_mi[-1]

    @classmethod
    def from_points(
        cls, points: list[tuple[float, float]], road_mi: float, duration_s: float
    ) -> RouteInfo:
        cum = [0.0]
        for a, b in zip(points, points[1:], strict=False):
            cum.append(cum[-1] + _haversine_mi(a, b))
        scale = road_mi / cum[-1] if cum[-1] > 0 else 1.0
        return cls(tuple(points), tuple(c * scale for c in cum), duration_s)

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """(road mile of the nearest point on the route, distance off the route in miles)."""
        best = (math.inf, 0.0)
        kx = math.cos(math.radians(lat))
        for i, (a, b) in enumerate(zip(self.points, self.points[1:], strict=False)):
            # Local flat projection, accurate enough for one polyline segment.
            ax, ay = (a[1] - lon) * kx, a[0] - lat
            bx, by = (b[1] - lon) * kx, b[0] - lat
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            px, py = ax + t * dx, ay + t * dy
            off = math.hypot(px, py) * math.pi / 180 * _EARTH_MI
            if off < best[0]:
                mile = self.cum_mi[i] + t * (self.cum_mi[i + 1] - self.cum_mi[i])
                best = (off, mile)
        return best[1], best[0]


def parse_duration(s: str) -> float:
    return float(s.rstrip("s"))


def money(m: dict[str, Any]) -> float:
    return int(m.get("units", 0) or 0) + int(m.get("nanos", 0) or 0) / 1e9


def decode_route(body: dict[str, Any]) -> RouteInfo:
    routes = body.get("routes") or []
    if not routes:
        raise ValueError("no route found")
    r = routes[0]
    pts = decode_polyline(r["polyline"]["encodedPolyline"])
    return RouteInfo.from_points(
        pts, r["distanceMeters"] / M_PER_MI, parse_duration(r.get("duration", "0s"))
    )


@dataclass(frozen=True)
class Segment:
    encoded: str
    origin: tuple[float, float]
    start_mi: float
    end_mi: float


def segments(route: RouteInfo, seg_mi: float = 100.0) -> list[Segment]:
    """Split the route into stretches for search-along-route.

    Each search returns at most 60 places, so a long route is searched in pieces.
    """
    out: list[Segment] = []
    start = 0
    n = len(route.points)
    while start < n - 1:
        end = start + 1
        while end < n - 1 and route.cum_mi[end] - route.cum_mi[start] < seg_mi:
            end += 1
        pts = list(route.points[start : end + 1])
        out.append(Segment(encode_polyline(pts), pts[0], route.cum_mi[start], route.cum_mi[end]))
        start = end
    return out


# --- stations ------------------------------------------------------------------


def brand_of(name: str) -> str | None:
    for brand, pat in _BRANDS:
        if pat.search(name):
            return brand
    return None


@dataclass(frozen=True)
class Candidate:
    place_id: str
    name: str
    state: str
    mile: float
    detour_mi: float
    price_per_gal: float
    """Posted price now (before member discount and drift)."""
    price_kind: str
    updated: datetime


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def decode_places(body: dict[str, Any], route: RouteInfo, seg: Segment) -> list[Candidate]:
    """One searchText page → stations selling diesel, located on the route."""
    places = body.get("places") or []
    summaries = body.get("routingSummaries") or []
    out: list[Candidate] = []
    for i, p in enumerate(places):
        prices = {fp.get("type"): fp for fp in (p.get("fuelOptions") or {}).get("fuelPrices", [])}
        kind = next((k for k in _DIESEL_KINDS if k in prices and "price" in prices[k]), None)
        loc = p.get("location") or {}
        if kind is None or "latitude" not in loc:
            continue
        mile, off = route.project(loc["latitude"], loc["longitude"])
        legs = (summaries[i] if i < len(summaries) else {}).get("legs") or []
        if len(legs) == 2 and all("distanceMeters" in leg for leg in legs):
            via = (legs[0]["distanceMeters"] + legs[1]["distanceMeters"]) / M_PER_MI
            detour = max(0.0, via - (seg.end_mi - seg.start_mi))
        else:
            detour = 2 * off
        m = _STATE_RE.search(p.get("formattedAddress", ""))
        fp = prices[kind]
        out.append(
            Candidate(
                place_id=str(p.get("id", "")),
                name=str((p.get("displayName") or {}).get("text", "?")),
                state=m.group(1) if m else "",
                mile=mile,
                detour_mi=detour,
                price_per_gal=money(fp["price"]),
                price_kind=kind,
                updated=_parse_time(fp["updateTime"]) if "updateTime" in fp else datetime.min,
            )
        )
    return out


# --- trend ---------------------------------------------------------------------


def decode_eia(body: dict[str, Any]) -> dict[str, list[tuple[date, float]]]:
    """EIA weekly rows → {duoarea: [(week, $/gal), ...] newest first}."""
    series: dict[str, list[tuple[date, float]]] = {}
    for row in (body.get("response") or {}).get("data", []):
        if row.get("value") in (None, ""):
            continue
        d = date.fromisoformat(row["period"])
        series.setdefault(row["duoarea"], []).append((d, float(row["value"])))
    for v in series.values():
        v.sort(reverse=True)
    return series


def drift_per_day(series: list[tuple[date, float]], weeks: int = 4) -> float:
    """Trend in $/gal/day over the last ``weeks`` (fewer if that's all there is), capped."""
    if len(series) < 2:
        return 0.0
    newest, oldest = series[0], series[min(weeks, len(series) - 1)]
    days = (newest[0] - oldest[0]).days
    if days <= 0:
        return 0.0
    d = (newest[1] - oldest[1]) / days
    return max(-DRIFT_CAP_PER_DAY, min(DRIFT_CAP_PER_DAY, d))


def region_for_state(state: str) -> str:
    if state == "CA":
        return "SCA"
    if state in _WEST_COAST_EX_CA:
        return "R5XCA"
    return "NUS"


# --- assembly ------------------------------------------------------------------


def build_stations(
    candidates: list[Candidate],
    route: RouteInfo,
    now: datetime,
    depart: datetime,
    drifts: dict[str, float],
    discounts: dict[str, float],
    max_age_h: float = 72.0,
) -> tuple[Station, ...]:
    """Candidates → planner stations: dedupe, drop stale prices, apply discount and trend."""
    seen: set[str] = set()
    out: list[Station] = []
    for c in sorted(candidates, key=lambda c: c.mile):
        if c.place_id in seen:
            continue
        seen.add(c.place_id)
        age_h = (now - c.updated).total_seconds() / 3600
        if age_h > max_age_h:
            continue
        eta = depart + timedelta(seconds=route.duration_s * c.mile / route.total_mi)
        days = max(0.0, (eta - now).total_seconds() / 86400)
        drift = drifts.get(region_for_state(c.state), drifts.get("NUS", 0.0)) * days
        brand = brand_of(c.name)
        disc = discounts.get(brand, 0.0) if brand else 0.0
        notes = [f"{age_h:.0f} h old"]
        if disc:
            notes.insert(0, f"Open Roads −${disc:.2f}")
        if c.price_kind != "DIESEL":
            notes.append(c.price_kind.lower())
        if abs(drift) >= 0.005:
            notes.append(f"trend {drift:+.2f}")
        out.append(
            Station(
                name=f"{c.name}, {c.state}" if c.state else c.name,
                mile=c.mile,
                price_per_gal=round(c.price_per_gal - disc + drift, 3),
                detour_mi=round(c.detour_mi, 1),
                detour_min=round(c.detour_mi / DETOUR_MPH * 60, 1),
                note="; ".join(notes),
            )
        )
    return tuple(out)


def mirror_return(
    stations: tuple[Station, ...], out_mi: float, local_mi: float
) -> tuple[Station, ...]:
    """Out-and-back on the same road: list each station again for the return leg."""
    down = tuple(
        Station(f"{s.name} ↓", s.mile, s.price_per_gal, s.detour_mi, s.detour_min, s.diesel, s.note)
        for s in stations
    )
    up = tuple(
        Station(
            f"{s.name} ↑",
            out_mi + local_mi + (out_mi - s.mile),
            s.price_per_gal,
            s.detour_mi,
            s.detour_min,
            s.diesel,
            s.note,
        )
        for s in stations
    )
    return down + up


def collect_candidates(
    route: RouteInfo,
    search: Callable[[Segment, str | None], dict[str, Any]],
    seg_mi: float = 100.0,
    max_pages: int = 3,
) -> list[Candidate]:
    """Search each stretch of the route (up to 3 pages of 20, the API's limit)."""
    out: list[Candidate] = []
    for seg in segments(route, seg_mi):
        token: str | None = None
        for _ in range(max_pages):
            body = search(seg, token)
            out.extend(decode_places(body, route, seg))
            token = body.get("nextPageToken") or None
            if not token:
                break
    return out


def parse_discounts(spec: str) -> dict[str, float]:
    """``"loves=0.30,ta=0.35"`` → {"loves": 0.30, "ta": 0.35}; ``"none"`` → {}."""
    if spec.strip().lower() in ("", "none"):
        return {}
    out: dict[str, float] = {}
    for part in spec.split(","):
        brand, _, amount = part.partition("=")
        brand = brand.strip().lower().replace("'", "")
        if brand not in BRAND_LABEL:
            raise ValueError(f"unknown brand {brand!r}; use {', '.join(BRAND_LABEL)}")
        out[brand] = float(amount)
    return out


@dataclass(frozen=True)
class LiveFuel:
    tank_gal: float
    mpg: float
    calibrating: bool
    stale: bool
    fill_pending: bool


def live_fuel_from_state(payload: bytes) -> LiveFuel:
    """bigair's retained ``bigair/fuel/state`` → the planner's starting point."""
    import json

    d = json.loads(payload)
    return LiveFuel(
        tank_gal=float(d["tank_gal"]),
        mpg=float(d["mpg_rolling"]),
        calibrating=bool(d.get("calibrating", False)),
        stale=bool(d.get("stale", False)),
        fill_pending=bool(d.get("fill_pending", False)),
    )
