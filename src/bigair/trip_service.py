"""A whole trip plan: route, fuel stops per reserve depth, weather along the way (#75).

Shared by the CLI (`bigair fuel-plan --from/--to`) and the planner web server
(`bigair planner`). Every network call comes in through ``TripEdges``, so this
module imports no edges; ``main.py`` wires the real ones in.

Failures are graded. With no route, no fuel data or stale fuel data, there is no
plan (``TripError``). Missing weather, alerts or the price trend only add a
warning, because the fuel plan stands without them.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from bigair.day_plan import Candidate, DayBudget, Proposal, day_budget, propose, score_poi
from bigair.destinations import (
    DEDUPE_MI,
    Destination,
    DestOption,
    crow_mi,
    dest_score,
    gather,
    rank,
)
from bigair.fuel_plan import (
    Plan,
    PlanInput,
    PlanRefusedError,
    default_home_price,
    naive_plan,
    plan_options,
)
from bigair.fuel_sources import (
    OPEN_ROADS_DEFAULT,
    RouteInfo,
    Unpriced,
    build_stations,
    collect_candidates,
    decode_eia,
    decode_polyline,
    decode_route,
    drift_per_day,
    encode_polyline,
    mirror_return,
)
from bigair.poi_near import LookupCache, PoiHit, Reach, near_route, reach_from_json
from bigair.route_weather import (
    CLASSIFY,
    WMO_LABELS,
    Alert,
    Hourly,
    RoutePoint,
    classify,
    conditions_at,
    decode_nws_alerts,
    decode_open_meteo,
    return_leg_points,
    sample_points,
    wmo_label,
)
from bigair.travel_rules import TravelRules

if TYPE_CHECKING:
    from collections.abc import Callable

    from bigair.fuel_plan import Station
    from bigair.fuel_sources import LiveFuel, Segment
    from bigair.poi import Poi
    from bigair.poi_near import DriveTime

HERE = ("here", "current location", "current")
DEFAULT_RESERVES = (150.0, 100.0, 75.0, 50.0)
ALERT_EVERY = 2
MODES = ("quick", "slow")
"""Ask NWS about every other weather point (about every 50 mi)."""


class TripError(RuntimeError):
    """No trustworthy plan: the message says why."""


@dataclass(frozen=True)
class TripRequest:
    origin: str
    destination: str
    via: tuple[str, ...] = ()
    round_trip: bool = False
    local_mi: float = 20.0
    depart: datetime | None = None
    return_depart: datetime | None = None
    reserves: tuple[float, ...] = DEFAULT_RESERVES
    discounts: dict[str, float] = field(default_factory=lambda: dict(OPEN_ROADS_DEFAULT))
    tank_gal: float | None = None
    mpg: float | None = None
    calibrating: bool = False
    capacity_gal: float = 47.0
    stop_penalty_usd: float = 10.0
    home_price: float | None = None
    max_age_h: float = 72.0
    weather_every_mi: float = 25.0
    reach: Reach = Reach("detour", 3.0)
    mode: str = "quick"
    """``quick`` (Google's route) or ``slow`` (travel rules, scenic proposal, #90)."""
    utc_offset_min: int | None = None
    """The phone's UTC offset, for slow mode's local times. None: the Pi's."""


@dataclass(frozen=True)
class TripEdges:
    route: Callable[[str, str, list[str], bool], dict[str, Any]]
    """(origin, destination, via, avoid_highways) → Google route body."""
    search: Callable[[Segment, str | None, str, str | None], dict[str, Any]]
    now: Callable[[], datetime]
    eia: Callable[[], dict[str, Any]] | None = None
    forecasts: Callable[[list[tuple[float, float]]], list[dict[str, Any]]] | None = None
    alerts: Callable[[float, float], dict[str, Any]] | None = None
    fuel_state: Callable[[], LiveFuel | None] | None = None
    position: Callable[[], tuple[float, float] | None] | None = None
    pois: Callable[[], tuple[list[Poi], list[str]]] | None = None
    """The vault's POI notes and any load warnings (#88). None: no POI folder."""
    drive_s: DriveTime | None = None
    """Drive seconds between two points, for POI detours."""
    poi_cache: LookupCache = field(default_factory=dict)
    """Detour lookups already paid for, reused when the reach selector changes."""
    ioverlander: Callable[[], tuple[list[Destination], list[str]]] | None = None
    """Dan's iOverlander GPX exports (#91)."""
    public_land: Callable[[], tuple[list[Destination], list[str]]] | None = None
    """BLM / USFS units from a local GeoJSON (#91)."""
    campgrounds: Callable[[tuple[float, float, float, float]], list[Destination]] | None = None
    """Google Places campgrounds in a (south, west, north, east) box: the fallback."""


@dataclass(frozen=True)
class TripResult:
    request: TripRequest
    generated_at: datetime
    depart: datetime
    return_depart: datetime | None
    route: RouteInfo
    trip_mi: float
    stations: tuple[Station, ...]
    unpriced: list[Unpriced]
    inp: PlanInput
    options: list[Plan]
    naive: Plan
    points: list[RoutePoint]
    hourly: list[Hourly]
    alerts: list[Alert]
    warnings: tuple[str, ...]
    pois: list[PoiHit] = field(default_factory=list)
    pois_enabled: bool = False
    scores: dict[str, tuple[float, list[str]]] = field(default_factory=dict)
    day: DayPlan | None = None


@dataclass(frozen=True)
class DayPlan:
    """Slow mode's day: the time budget and the proposed stops (#90)."""

    budget: DayBudget
    drive_min: float
    route_mi: float
    rules: TravelRules
    proposal: Proposal


def _slow_depart(now: datetime, rules: TravelRules, offset_min: int | None) -> datetime:
    """The next departure at the rules' time (10:00), local to the phone (or the Pi)."""
    tz = timezone(timedelta(minutes=offset_min)) if offset_min is not None else None
    local = now.astimezone(tz)
    d = local.replace(hour=rules.depart_h, minute=rules.depart_m, second=0, microsecond=0)
    return d if d >= local else d + timedelta(days=1)


def plan_trip(req: TripRequest, edges: TripEdges, rules: TravelRules | None = None) -> TripResult:
    rules = rules or TravelRules()
    slow = req.mode == "slow"
    now = edges.now()
    warnings: list[str] = []

    origin = req.origin
    if origin.strip().lower() in HERE:
        pos = edges.position() if edges.position else None
        if pos is None:
            raise TripError("no GPS fix for the current location; type a starting place")
        origin = f"{pos[0]:.5f},{pos[1]:.5f}"

    try:
        avoid = slow and rules.avoid_interstates
        route = decode_route(edges.route(origin, req.destination, list(req.via), avoid))
    except ValueError as e:
        raise TripError(f"no route from {req.origin} to {req.destination}") from e

    tank, mpg, calibrating, stale = req.tank_gal, req.mpg, req.calibrating, False
    if edges.fuel_state is not None:
        live = edges.fuel_state()
        if live is None and (tank is None or mpg is None):
            raise TripError("no fuel state from bigair; give the tank and mpg")
        if live is not None:
            tank = live.tank_gal if tank is None else tank
            mpg = live.mpg if mpg is None else mpg
            calibrating = calibrating or live.calibrating
            stale = live.stale
            if live.fill_pending:
                warnings.append("A fill is pending, so the tank reads low until it's confirmed.")
    if tank is None or mpg is None:
        raise TripError("need the tank and mpg (or live fuel state)")

    drifts: dict[str, float] = {}
    if edges.eia is not None:
        try:
            series = decode_eia(edges.eia())
            drifts = {area: drift_per_day(v) for area, v in series.items()}
        except (OSError, ValueError, KeyError) as e:
            warnings.append(f"Price trend unavailable ({e}); planned without it.")

    if req.depart is not None:
        depart = req.depart
    elif slow:
        depart = _slow_depart(now, rules, req.utc_offset_min)
    else:
        depart = now
    found = collect_candidates(route, edges.search)
    stations = build_stations(
        found.candidates, route, now, depart, drifts, req.discounts, req.max_age_h
    )
    out_s = route.duration_s
    return_depart: datetime | None = None
    if req.round_trip:
        trip_mi = 2 * route.total_mi + req.local_mi
        stations = mirror_return(stations, route.total_mi, req.local_mi)
        return_depart = req.return_depart or depart + timedelta(seconds=out_s, hours=2)
    else:
        trip_mi = route.total_mi

    home = default_home_price(stations, trip_mi) if req.home_price is None else req.home_price
    inp = PlanInput(
        route_mi=trip_mi,
        stations=stations,
        start_gal=tank,
        mpg=mpg,
        capacity_gal=req.capacity_gal,
        stop_penalty_usd=req.stop_penalty_usd,
        end_value_per_gal=home,
        calibrating=calibrating,
        stale=stale,
    )
    try:
        options = plan_options(inp, req.reserves)
        naive = naive_plan(inp)
    except PlanRefusedError as e:
        raise TripError(str(e)) from e

    points: list[RoutePoint] = []
    hourly: list[Hourly] = []
    alerts: list[Alert] = []
    if edges.forecasts is not None:
        out_pts = sample_points(route, req.weather_every_mi)
        try:
            hourly = decode_open_meteo(edges.forecasts([(p.lat, p.lon) for p in out_pts]))
            points = out_pts
            if req.round_trip:
                points = out_pts + return_leg_points(out_pts, route.total_mi, req.local_mi, out_s)
        except (OSError, ValueError, KeyError) as e:
            warnings.append(f"Weather unavailable ({e}).")
        if edges.alerts is not None and out_pts:
            try:
                picks = out_pts[::ALERT_EVERY] + [out_pts[-1]]
                alerts = decode_nws_alerts([edges.alerts(p.lat, p.lon) for p in picks])
            except (OSError, ValueError, KeyError) as e:
                warnings.append(f"Weather alerts unavailable ({e}).")

    hits, pois_enabled = _pois(route, depart, req.reach, edges, warnings)
    temp = _temp_from_forecasts(points, hourly)
    scores = {h.poi.name: score_poi(h.poi, h.in_season, temp(h.mile, h.eta), rules) for h in hits}
    hits = _by_score(hits, scores)
    day = None
    if slow:
        budget = day_budget(depart, rules, route.points[-1])
        drive_min = route.duration_s / 60
        day = DayPlan(
            budget,
            drive_min,
            route.total_mi,
            rules,
            propose(_candidates(hits, scores), drive_min, route.total_mi, budget.window_min, rules),
        )

    return TripResult(
        request=req,
        generated_at=now,
        depart=depart,
        return_depart=return_depart,
        route=route,
        trip_mi=trip_mi,
        stations=stations,
        unpriced=found.unpriced,
        inp=inp,
        options=options,
        naive=naive,
        points=points,
        hourly=hourly,
        alerts=alerts,
        warnings=tuple(warnings),
        pois=hits,
        pois_enabled=pois_enabled,
        scores=scores,
        day=day,
    )


def _f(temp_c: float) -> float:
    return temp_c * 9 / 5 + 32


def _temp_from_forecasts(
    points: list[RoutePoint], hourly: list[Hourly]
) -> Callable[[float, datetime], float | None]:
    """°F at the outbound weather point nearest a road mile, at a time; None if unknown."""
    out = [p for p in points if p.leg == "out"]

    def at(mile: float, when: datetime) -> float | None:
        if not out:
            return None
        p = min(out, key=lambda q: abs(q.mile - mile))
        if p.series >= len(hourly):
            return None
        c = conditions_at(hourly[p.series], when)
        return None if c is None else _f(c.temp_c)

    return at


def _temp_from_json(last: dict[str, Any]) -> Callable[[float, datetime], float | None]:
    """The same lookup, from a cached plan's weather JSON."""
    w = last.get("weather") or {}
    out = [p for p in (w.get("points") or []) if p.get("leg") == "out"]
    series = w.get("series") or []

    def at(mile: float, when: datetime) -> float | None:
        if not out:
            return None
        p = min(out, key=lambda q: abs(float(q["mile"]) - mile))
        i = int(p["series"])
        if i >= len(series) or not series[i].get("start"):
            return None
        s = series[i]
        k = round((when - datetime.fromisoformat(s["start"])).total_seconds() / 3600)
        temps = s.get("temp_c") or []
        return _f(float(temps[k])) if 0 <= k < len(temps) else None

    return at


def _by_score(hits: list[PoiHit], scores: dict[str, tuple[float, list[str]]]) -> list[PoiHit]:
    """Best score first; then known detours, shortest first (#90)."""

    def key(h: PoiHit) -> tuple[float, int, float]:
        d = h.detour_min
        return (-scores[h.poi.name][0], 0 if d is not None else 1, d if d is not None else 0.0)

    return sorted(hits, key=key)


def _candidates(hits: list[PoiHit], scores: dict[str, tuple[float, list[str]]]) -> list[Candidate]:
    """Only POIs whose detour time, detour miles and visit time are all known."""
    return [
        Candidate(
            h.poi.name, scores[h.poi.name][0], h.detour_min, float(h.poi.visit_min), h.detour_mi
        )
        for h in hits
        if h.detour_min is not None and h.detour_mi is not None and h.poi.visit_min is not None
    ]


def _pois(
    route: RouteInfo, depart: datetime, reach: Reach, edges: TripEdges, warnings: list[str]
) -> tuple[list[PoiHit], bool]:
    """POIs near the outbound route. Never fails the plan: problems become warnings."""
    if edges.pois is None or edges.drive_s is None:
        return [], False
    pois, problems = edges.pois()
    warnings.extend(problems)
    return near_route(route, depart, pois, reach, edges.drive_s, edges.poi_cache), True


def pois_for_cached(
    last: dict[str, Any], reach: Reach, edges: TripEdges, rules: TravelRules | None = None
) -> dict[str, Any]:
    """Re-run the POI search on the cached plan's route with a new reach (no re-plan).

    In slow mode the proposal is recomputed too, against the cached day budget.
    """
    rules = rules or TravelRules()
    try:
        route = RouteInfo.from_points(
            decode_polyline(last["route"]["polyline"]),
            float(last["route"]["one_way_mi"]),
            float(last["route"]["duration_s"]),
        )
        depart = datetime.fromisoformat(last["depart"])
    except (KeyError, TypeError, ValueError) as e:
        raise TripError("the cached plan has no usable route; plan again") from e
    warnings: list[str] = []
    hits, enabled = _pois(route, depart, reach, edges, warnings)
    temp = _temp_from_json(last)
    scores = {h.poi.name: score_poi(h.poi, h.in_season, temp(h.mile, h.eta), rules) for h in hits}
    hits = _by_score(hits, scores)
    block = _pois_json(hits, enabled, reach, scores, warnings)
    day = last.get("day") or {}
    if day.get("mode") == "slow":
        p = propose(
            _candidates(hits, scores),
            float(day["drive_min"]),
            float(day["route_mi"]),
            float(day["window_min"]),
            rules,
        )
        block["proposal"] = _proposal_json(p)
    return block


def _proposal_json(p: Proposal) -> dict[str, Any]:
    return {
        "fits": p.fits,
        "chosen": list(p.chosen),
        "added_min": round(p.added_min),
        "added_mi": round(p.added_mi),
        "score": round(p.score, 2),
        "problems": list(p.problems),
    }


def _day_json(day: DayPlan | None) -> dict[str, Any]:
    if day is None:
        return {"mode": "quick"}
    b, r = day.budget, day.rules
    return {
        "mode": "slow",
        "depart": _iso(b.depart),
        "arrive_by": _iso(b.arrive_by),
        "sunset": _iso(b.sunset),
        "end": _iso(b.end),
        "end_reason": b.end_reason,
        "window_min": round(b.window_min),
        "drive_min": round(day.drive_min),
        "route_mi": round(day.route_mi),
        "max_day_mi": r.max_day_mi,
        "stay_nights": r.stay_nights,
        "proposal": _proposal_json(day.proposal),
    }


def _pois_json(
    hits: list[PoiHit],
    enabled: bool,
    reach: Reach,
    scores: dict[str, tuple[float, list[str]]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    scores = scores or {}
    return {
        "enabled": enabled,
        "reach": {"mode": reach.mode, "hours": reach.hours},
        "warnings": warnings or [],
        "hits": [
            {
                "name": h.poi.name,
                "lat": h.poi.lat,
                "lon": h.poi.lon,
                "tags": list(h.poi.tags),
                "summary": h.poi.summary,
                "mile": round(h.mile, 1),
                "eta": _iso(h.eta),
                "off_route_mi": round(h.off_route_mi, 1),
                "status": h.status,
                "one_way_min": None if h.one_way_min is None else round(h.one_way_min),
                "detour_min": None if h.detour_min is None else round(h.detour_min),
                "detour_mi": None if h.detour_mi is None else round(h.detour_mi),
                "visit_min": h.poi.visit_min,
                "visit_what": h.poi.visit_what,
                "best_months": None if h.poi.best_months is None else list(h.poi.best_months),
                "in_season": h.in_season,
                "rating": h.poi.rating,
                "destination": h.poi.destination,
                "score": round(scores[h.poi.name][0], 2) if h.poi.name in scores else None,
                "reasons": scores[h.poi.name][1] if h.poi.name in scores else [],
            }
            for h in hits
        ],
    }


# --- destination discovery (#91) --------------------------------------------------

MAX_ROUTED_DESTINATIONS = 8
"""Candidates routed per search (each is one Google route plus a few detour lookups)."""
DEST_LOOKUPS = 10
"""Detour lookups per candidate leg."""


@dataclass(frozen=True)
class DiscoverRequest:
    origin: str
    depart: datetime | None = None
    utc_offset_min: int | None = None
    reach: Reach = Reach("detour", 3.0)


@dataclass(frozen=True)
class DiscoverResult:
    request: DiscoverRequest
    generated_at: datetime
    origin: tuple[float, float]
    depart: datetime
    options: list[DestOption]
    warnings: tuple[str, ...]


def _resolve_origin(origin: str, edges: TripEdges) -> tuple[str, tuple[float, float] | None]:
    if origin.strip().lower() in HERE:
        pos = edges.position() if edges.position else None
        if pos is None:
            raise TripError("no GPS fix for the current location; type a starting place")
        return f"{pos[0]:.5f},{pos[1]:.5f}", pos
    return origin, None


def _load(fn: Callable[[], tuple[list[Any], list[str]]] | None, warnings: list[str]) -> list[Any]:
    if fn is None:
        return []
    items, problems = fn()
    warnings.extend(problems)
    return items


def _bbox(origin: tuple[float, float], miles: float) -> tuple[float, float, float, float]:
    dlat = miles / 69.0
    dlon = miles / (69.0 * max(0.2, math.cos(math.radians(origin[0]))))
    return (origin[0] - dlat, origin[1] - dlon, origin[0] + dlat, origin[1] + dlon)


def discover(
    req: DiscoverRequest, edges: TripEdges, rules: TravelRules | None = None
) -> DiscoverResult:
    """Rank where to spend the next few nights, from where the van is."""
    rules = rules or TravelRules()
    now = edges.now()
    warnings: list[str] = []
    origin_str, pos = _resolve_origin(req.origin, edges)
    depart = req.depart or _slow_depart(now, rules, req.utc_offset_min)

    pois = _load(edges.pois, warnings)
    iover = _load(edges.ioverlander, warnings)
    land = _load(edges.public_land, warnings)

    # Find the origin's coordinates: GPS, "lat,lon", or the first routed leg's start.
    origin_pt = pos or _latlon(origin_str)
    if origin_pt is None:
        probe = pois[0] if pois else (iover + land)[0] if (iover or land) else None
        if probe is None:
            warnings.append("No destinations in range: nothing to search from.")
            return DiscoverResult(req, now, (0.0, 0.0), depart, [], tuple(warnings))
        try:
            r0 = decode_route(edges.route(origin_str, f"{probe.lat},{probe.lon}", [], False))
        except ValueError as e:
            raise TripError(f"couldn't find {req.origin}") from e
        origin_pt = r0.points[0]

    cands = gather(origin_pt, pois, iover, land, rules)
    if not cands and edges.campgrounds is not None:
        try:
            camps = edges.campgrounds(_bbox(origin_pt, rules.max_day_mi))
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            warnings.append(f"Campground search failed ({e}).")
            camps = []
        cands = gather(origin_pt, [], [], [], rules, camps)
    if not cands:
        warnings.append(
            f"No destinations within {rules.max_day_mi:.0f} mi: add POIs with destination: true, "
            "iOverlander exports, or public land."
        )
        return DiscoverResult(req, now, origin_pt, depart, [], tuple(warnings))

    def season(c: Destination, when: datetime) -> bool | None:
        if c.poi is None or c.poi.best_months is None:
            return None
        return when.month in c.poi.best_months

    # Route the most promising: best own score first, then the closest.
    cands.sort(
        key=lambda c: (
            -dest_score(c, season(c, depart), None, rules)[0],
            crow_mi(origin_pt, (c.lat, c.lon)),
        )
    )
    routed: list[tuple[Destination, RouteInfo]] = []
    for c in cands[:MAX_ROUTED_DESTINATIONS]:
        try:
            body = edges.route(origin_str, f"{c.lat},{c.lon}", [], rules.avoid_interstates)
            routed.append((c, decode_route(body)))
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            warnings.append(f"No route to {c.name} ({e}).")

    temps: list[Hourly] = []
    if edges.forecasts is not None and routed:
        try:
            temps = decode_open_meteo(edges.forecasts([(c.lat, c.lon) for c, _ in routed]))
        except (OSError, ValueError, KeyError) as e:
            warnings.append(f"Weather unavailable ({e}).")

    options: list[DestOption] = []
    for i, (c, route) in enumerate(routed):
        arrive = depart + timedelta(seconds=route.duration_s)
        cond = conditions_at(temps[i], arrive) if i < len(temps) else None
        temp_f = None if cond is None else _f(cond.temp_c)
        ds, reasons = dest_score(c, season(c, arrive), temp_f, rules)
        hits: list[PoiHit] = []
        if edges.drive_s is not None:
            hits = near_route(
                route, depart, pois, req.reach, edges.drive_s, edges.poi_cache, DEST_LOOKUPS
            )
            # The destination itself isn't a stop on the way to it.
            hits = [h for h in hits if crow_mi((h.poi.lat, h.poi.lon), (c.lat, c.lon)) > DEDUPE_MI]
        scores = {h.poi.name: score_poi(h.poi, h.in_season, None, rules) for h in hits}
        budget = day_budget(depart, rules, (c.lat, c.lon))
        drive_min = route.duration_s / 60
        p = propose(_candidates(hits, scores), drive_min, route.total_mi, budget.window_min, rules)
        options.append(
            DestOption(c, route.total_mi, drive_min, arrive, ds, reasons, p, ds + p.score)
        )
    return DiscoverResult(req, now, origin_pt, depart, rank(options), tuple(warnings))


_LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def _latlon(s: str) -> tuple[float, float] | None:
    m = _LATLON_RE.match(s)
    return (float(m.group(1)), float(m.group(2))) if m else None


def discover_to_json(r: DiscoverResult) -> dict[str, Any]:
    return {
        "generated_at": _iso(r.generated_at),
        "origin": {"query": r.request.origin, "lat": r.origin[0], "lon": r.origin[1]},
        "depart": _iso(r.depart),
        "warnings": list(r.warnings),
        "options": [
            {
                "name": o.dest.name,
                "source": o.dest.source,
                "kind": o.dest.kind,
                "note": o.dest.note,
                "lat": round(o.dest.lat, 5),
                "lon": round(o.dest.lon, 5),
                "road_mi": round(o.road_mi),
                "drive_min": round(o.drive_min),
                "arrive": _iso(o.arrive),
                "dest_score": round(o.dest_score, 2),
                "dest_reasons": o.dest_reasons,
                "stops": list(o.drive.chosen),
                "stops_score": round(o.drive.score, 2),
                "stops_min": round(o.drive.added_min),
                "fits": o.drive.fits,
                "problems": list(o.drive.problems),
                "total": round(o.total, 2),
            }
            for o in r.options
        ],
    }


def discover_request_from_json(d: dict[str, Any]) -> DiscoverRequest:
    origin = str(d.get("origin") or "").strip()
    if not origin:
        raise TripError("give a starting place (or Here)")
    v = d.get("depart")
    depart = None
    if v:
        try:
            depart = datetime.fromisoformat(str(v))
        except ValueError as e:
            raise TripError("depart must be an ISO date and time") from e
        if depart.tzinfo is None:
            raise TripError("depart needs a time zone offset")
    offset_in = d.get("utc_offset_min")
    offset = None if offset_in in (None, "") else round(_num(offset_in, "utc_offset_min"))
    reach_in = d.get("reach")
    try:
        reach = reach_from_json(reach_in if isinstance(reach_in, dict) else None)
    except ValueError as e:
        raise TripError(str(e)) from e
    return DiscoverRequest(
        origin=origin, depart=_local(depart, offset), utc_offset_min=offset, reach=reach
    )


# --- presentation -----------------------------------------------------------------


def leg_start(r: TripResult, leg: str) -> datetime:
    return r.return_depart if leg == "back" and r.return_depart else r.depart


def weather_summary(r: TripResult) -> list[str]:
    """One line per leg: how much of it is clear/wet/snow/severe, and the worst spot."""
    rank = {"clear": 0, "wet": 1, "snow": 2, "severe": 3}
    lines: list[str] = []
    for leg in ("out", "back"):
        pts = [p for p in r.points if p.leg == leg]
        if not pts:
            continue
        counts: Counter[str] = Counter()
        worst: tuple[int, RoutePoint, str, datetime] | None = None
        unknown = 0
        for p in pts:
            when = leg_start(r, leg) + timedelta(seconds=p.offset_s)
            c = conditions_at(r.hourly[p.series], when) if p.series < len(r.hourly) else None
            if c is None:
                unknown += 1
                continue
            cls = classify(c)
            counts[cls] += 1
            if worst is None or rank[cls] > worst[0]:
                worst = (rank[cls], p, wmo_label(c.code), when)
        name = "Out" if leg == "out" else "Back"
        parts = [f"{counts[k]} {k}" for k in ("clear", "wet", "snow", "severe") if counts[k]]
        if unknown:
            parts.append(f"{unknown} beyond the forecast")
        line = f"{name}: " + ", ".join(parts)
        if worst is not None and worst[0] > 0:
            when = worst[3].astimezone(leg_start(r, leg).tzinfo)
            line += f"; worst: {worst[2]} near mi {worst[1].mile:.0f} ({when:%a %H:%M %Z})"
        lines.append(line)
    return lines


def _r1(vals: tuple[float, ...]) -> list[float]:
    return [round(v, 1) for v in vals]


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _plan_json(p: Plan, out_mi: float, round_trip: bool) -> dict[str, Any]:
    return {
        "reserve_mi": p.reserve_mi,
        "feasible": p.feasible,
        "reason": p.reason,
        "net_cost_usd": round(p.net_cost_usd, 2) if p.feasible else None,
        "fuel_cost_usd": round(p.fuel_cost_usd, 2),
        "min_gal": p.min_gal,
        "end_gal": p.end_gal,
        "stops": [
            {
                "name": s.station.name,
                "mile": round(s.station.mile, 1),
                "leg": "back" if round_trip and s.station.mile > out_mi else "out",
                "lat": s.station.lat,
                "lon": s.station.lon,
                "arrive_gal": s.arrive_gal,
                "buy_gal": s.buy_gal,
                "price_per_gal": s.station.price_per_gal,
                "cost_usd": round(s.cost_usd, 2),
                "note": s.station.note,
            }
            for s in p.stops
        ],
    }


def result_to_json(r: TripResult) -> dict[str, Any]:
    """Everything the planner page draws, as plain JSON."""
    rt = r.request.round_trip
    return {
        "generated_at": _iso(r.generated_at),
        "request": {
            "origin": r.request.origin,
            "destination": r.request.destination,
            "via": list(r.request.via),
            "round_trip": rt,
            "local_mi": r.request.local_mi,
            "reserves": list(r.request.reserves),
        },
        "depart": _iso(r.depart),
        "return_depart": _iso(r.return_depart),
        "route": {
            "polyline": encode_polyline(list(r.route.points)),
            "one_way_mi": round(r.route.total_mi, 1),
            "duration_s": r.route.duration_s,
        },
        "trip_mi": round(r.trip_mi, 1),
        "tank": {
            "start_gal": r.inp.start_gal,
            "mpg": r.inp.mpg,
            "planning_mpg": round(r.inp.planning_mpg, 2),
            "calibrating": r.inp.calibrating,
            "capacity_gal": r.inp.capacity_gal,
        },
        "home_price_per_gal": round(r.inp.end_value_per_gal, 3),
        "options": [_plan_json(p, r.route.total_mi, rt) for p in r.options],
        "naive": _plan_json(r.naive, r.route.total_mi, rt),
        "unpriced": [{"name": u.name, "where": u.where, "mile": round(u.mile)} for u in r.unpriced],
        "weather": {
            "points": [
                {
                    "mile": round(p.mile, 1),
                    "lat": round(p.lat, 5),
                    "lon": round(p.lon, 5),
                    "offset_s": round(p.offset_s),
                    "leg": p.leg,
                    "series": p.series,
                }
                for p in r.points
            ],
            "series": [
                {
                    "start": _iso(h.times[0]) if h.times else None,
                    "elevation_m": h.elevation_m,
                    "temp_c": _r1(h.temp_c),
                    "precip_prob_pct": [round(v) for v in h.precip_prob_pct],
                    "precip_mm": _r1(h.precip_mm),
                    "snow_cm": _r1(h.snow_cm),
                    "wind_mph": [round(v) for v in h.wind_mph],
                    "gust_mph": [round(v) for v in h.gust_mph],
                    "code": h.code,
                }
                for h in r.hourly
            ],
            "classify": CLASSIFY,
            "labels": WMO_LABELS,
        },
        "alerts": [
            {
                "event": a.event,
                "severity": a.severity,
                "headline": a.headline,
                "area": a.area,
                "onset": _iso(a.onset),
                "ends": _iso(a.ends),
            }
            for a in r.alerts
        ],
        "warnings": list(r.warnings),
        "pois": _pois_json(r.pois, r.pois_enabled, r.request.reach, r.scores),
        "day": _day_json(r.day),
    }


def _local(t: datetime | None, offset_min: int | None) -> datetime | None:
    """Read a time in the phone's UTC offset: the page sends UTC ("…Z"), and slow
    mode's 10:00 / 15:00 must mean local time, not UTC (#90)."""
    if t is None or offset_min is None:
        return t
    return t.astimezone(timezone(timedelta(minutes=offset_min)))


def _num(v: object, name: str) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise TripError(f"{name} must be a number") from e


def request_from_json(d: dict[str, Any]) -> TripRequest:
    """Validate the planner page's form."""
    origin = str(d.get("origin") or "").strip()
    dest = str(d.get("destination") or "").strip()
    if not origin or not dest:
        raise TripError("give a start and a destination")

    def when(key: str) -> datetime | None:
        v = d.get(key)
        if not v:
            return None
        try:
            t = datetime.fromisoformat(str(v))
        except ValueError as e:
            raise TripError(f"{key} must be an ISO date and time") from e
        if t.tzinfo is None:
            raise TripError(f"{key} needs a time zone offset")
        return t

    reserves = tuple(_num(x, "reserve") for x in (d.get("reserves") or DEFAULT_RESERVES))
    if not reserves or any(r < 0 for r in reserves):
        raise TripError("reserves must be zero or more miles")
    discounts = d.get("discounts")
    mode = str(d.get("mode") or "quick")
    if mode not in MODES:
        raise TripError("mode must be quick or slow")
    offset_in = d.get("utc_offset_min")
    offset = None if offset_in in (None, "") else round(_num(offset_in, "utc_offset_min"))
    reach_in = d.get("reach")
    try:
        reach = reach_from_json(reach_in if isinstance(reach_in, dict) else None)
    except ValueError as e:
        raise TripError(str(e)) from e
    return TripRequest(
        origin=origin,
        destination=dest,
        via=tuple(str(v).strip() for v in (d.get("via") or []) if str(v).strip()),
        round_trip=bool(d.get("round_trip")),
        local_mi=_num(d.get("local_mi", 20.0), "local_mi"),
        depart=_local(when("depart"), offset),
        return_depart=_local(when("return_depart"), offset),
        reserves=reserves,
        discounts=(
            {str(k): _num(v, "discount") for k, v in discounts.items()}
            if isinstance(discounts, dict)
            else dict(OPEN_ROADS_DEFAULT)
        ),
        tank_gal=_num(d["tank_gal"], "tank_gal") if d.get("tank_gal") not in (None, "") else None,
        mpg=_num(d["mpg"], "mpg") if d.get("mpg") not in (None, "") else None,
        reach=reach,
        mode=mode,
        utc_offset_min=offset,
    )
