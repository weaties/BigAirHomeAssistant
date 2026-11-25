"""Which POIs are near a planned route, and what visiting them costs (#88).

Three ways to say how far off the route to look (the reach selector):

- ``detour``: the round trip from the route to the POI and back is at most
  ``hours`` of driving.
- ``each_way``: the POI is at most ``hours`` of driving from the route.
- ``ahead``: the van reaches the POI's turn-off within the first ``hours`` of
  the route, and the POI is at most ``AHEAD_MAX_ONE_WAY_MIN`` off the route.

Each POI joins the route at its nearest point. A local straight-line prefilter
drops POIs that can't be in reach even at ``CROW_MPH``. That speed is a lower
bound on the time, so it never drops a reachable POI. The shortlist then gets
exact drive times from the ``drive_s`` edge (Google Routes), nearest first, at
most ``MAX_LOOKUPS`` per call. POIs past the cap are listed as ``unchecked``,
and failed lookups as ``lookup_failed``. Neither ever gets a guessed number.

The round-trip detour is twice the one-way drive from the join point. The real
detour can be shorter if a different road leads back, so this errs long, which
is the safe direction for a 3pm arrival.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from bigair.fuel_sources import RouteInfo
    from bigair.poi import Poi

MODES = ("detour", "each_way", "ahead")
DEFAULT_REACH_H = 3.0
MAX_REACH_H = 12.0
CROW_MPH = 70.0
AHEAD_MAX_ONE_WAY_MIN = 90.0
MAX_LOOKUPS = 25

DriveTime = Callable[[tuple[float, float], tuple[float, float]], float]
LookupCache = dict[tuple[float, float, float, float], float]


@dataclass(frozen=True)
class Reach:
    mode: str
    hours: float


def reach_from_json(d: dict[str, Any] | None) -> Reach:
    """The page's selector; raises ValueError on nonsense."""
    if not d:
        return Reach("detour", DEFAULT_REACH_H)
    mode = str(d.get("mode") or "detour")
    if mode not in MODES:
        raise ValueError(f"reach mode must be one of {', '.join(MODES)}")
    try:
        hours = float(d.get("hours", DEFAULT_REACH_H))
    except (TypeError, ValueError) as e:
        raise ValueError("reach hours must be a number") from e
    if not 0 < hours <= MAX_REACH_H:
        raise ValueError(f"reach hours must be between 0 and {MAX_REACH_H:g}")
    return Reach(mode, hours)


@dataclass(frozen=True)
class PoiHit:
    poi: Poi
    mile: float
    """Road mile where the POI's detour leaves the route."""
    eta: datetime
    """When the van reaches that turn-off, driving straight through."""
    off_route_mi: float
    """Straight-line distance from the route."""
    status: str
    """``ok``, ``unchecked`` (over the lookup cap) or ``lookup_failed``."""
    one_way_min: float | None
    detour_min: float | None
    in_season: bool | None
    """Is the ETA's month in ``best_months``? None if the note doesn't say."""


def point_at(route: RouteInfo, mile: float) -> tuple[float, float]:
    """The lat/lon at a road mile along the route."""
    cum, pts = route.cum_mi, route.points
    if len(pts) == 1:
        return pts[0]
    j = 0
    while j < len(cum) - 2 and cum[j + 1] < mile:
        j += 1
    span = cum[j + 1] - cum[j]
    f = 0.0 if span <= 0 else max(0.0, min(1.0, (mile - cum[j]) / span))
    a, b = pts[j], pts[j + 1]
    return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))


def _min_one_way(off_mi: float) -> float:
    return off_mi / CROW_MPH * 60.0


def _fits(reach: Reach, one_way_min: float, join_offset_min: float) -> bool:
    budget = reach.hours * 60.0
    if reach.mode == "detour":
        return 2 * one_way_min <= budget
    if reach.mode == "each_way":
        return one_way_min <= budget
    return join_offset_min <= budget and one_way_min <= AHEAD_MAX_ONE_WAY_MIN


def near_route(
    route: RouteInfo,
    depart: datetime,
    pois: list[Poi],
    reach: Reach,
    drive_s: DriveTime,
    cache: LookupCache | None = None,
) -> list[PoiHit]:
    """POIs in reach of the route, ordered by detour, then rating, then season."""
    cache = {} if cache is None else cache
    total = route.total_mi
    candidates: list[tuple[float, float, float, Poi]] = []  # off, mile, offset_min, poi
    for p in pois:
        mile, off = route.project(p.lat, p.lon)
        offset_min = route.duration_s * (mile / total) / 60.0 if total else 0.0
        if _fits(reach, _min_one_way(off), offset_min):
            candidates.append((off, mile, offset_min, p))
    candidates.sort(key=lambda c: c[0])

    hits: list[PoiHit] = []
    lookups = 0
    for off, mile, offset_min, p in candidates:
        join = point_at(route, mile)
        key = (round(join[0], 4), round(join[1], 4), round(p.lat, 4), round(p.lon, 4))
        eta = depart + timedelta(minutes=offset_min)
        season = None if p.best_months is None else eta.month in p.best_months
        one_way: float | None = None
        if key in cache:
            one_way = cache[key]
            status = "ok"
        elif lookups >= MAX_LOOKUPS:
            status = "unchecked"
        else:
            lookups += 1
            try:
                one_way = drive_s(join, (p.lat, p.lon)) / 60.0
                cache[key] = one_way
                status = "ok"
            except (OSError, ValueError, KeyError, RuntimeError):
                status = "lookup_failed"
        if one_way is not None and not _fits(reach, one_way, offset_min):
            continue
        hits.append(
            PoiHit(
                poi=p,
                mile=mile,
                eta=eta,
                off_route_mi=off,
                status=status,
                one_way_min=one_way,
                detour_min=None if one_way is None else 2 * one_way,
                in_season=season,
            )
        )

    def order(h: PoiHit) -> tuple[int, float, int, int, float]:
        season_rank = {True: 0, None: 1, False: 2}[h.in_season]
        return (
            0 if h.detour_min is not None else 1,
            h.detour_min if h.detour_min is not None else 0.0,
            -(h.poi.rating or 0),
            season_rank,
            h.off_route_mi,
        )

    return sorted(hits, key=order)
