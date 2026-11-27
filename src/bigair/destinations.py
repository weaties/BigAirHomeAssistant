"""Destination discovery (#91): where to spend the next few nights.

Candidates come from four sources, in order of preference when two are within
``DEDUPE_MI`` of each other:

1. vault POIs with ``destination: true`` (Dan's own picks);
2. iOverlander campsites (Dan's exports);
3. public-land units (BLM / USFS);
4. campgrounds from Google Places, only as a fallback when there's no dispersed
   option in range.

A straight-line prefilter keeps candidates within the daily mileage ceiling
(straight-line distance never exceeds road miles), and at least ``MIN_MOVE_MI``
away (otherwise it isn't a move). The ranking adds the destination's own score
to the score of the best stops on the way (S2's proposal for that leg).
Distance is never rewarded: a closer, richer destination beats a far one, and
ties go to fewer miles. Legs that don't fit sink to the bottom with their reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bigair.day_plan import score_poi, weather_term

if TYPE_CHECKING:
    from datetime import datetime

    from bigair.day_plan import Proposal
    from bigair.poi import Poi
    from bigair.travel_rules import TravelRules

DEDUPE_MI = 1.0
MIN_MOVE_MI = 5.0
SOURCE_RANK = {"poi": 0, "ioverlander": 1, "public_land": 2, "campground": 3}
CAMPGROUND_PENALTY = 0.25


@dataclass(frozen=True)
class Destination:
    name: str
    lat: float
    lon: float
    source: str
    """``poi``, ``ioverlander``, ``public_land`` or ``campground``."""
    kind: str | None
    """iOverlander category, land manager (BLM, USFS), or None."""
    note: str | None
    poi: Poi | None = None


def crow_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = math.radians
    h = (
        math.sin(r(b[0] - a[0]) / 2) ** 2
        + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(r(b[1] - a[1]) / 2) ** 2
    )
    return 2 * 3958.76 * math.asin(math.sqrt(h))


def gather(
    origin: tuple[float, float],
    pois: list[Poi],
    ioverlander: list[Destination],
    public_land: list[Destination],
    rules: TravelRules,
    campgrounds: list[Destination] | None = None,
) -> list[Destination]:
    """Candidates in range, deduped, in source-preference then distance order."""
    vault = [
        Destination(p.name, p.lat, p.lon, "poi", None, p.summary, p) for p in pois if p.destination
    ]
    pool = [*vault, *ioverlander, *public_land, *(campgrounds or [])]
    pool = [c for c in pool if MIN_MOVE_MI <= crow_mi(origin, (c.lat, c.lon)) <= rules.max_day_mi]
    pool.sort(key=lambda c: (SOURCE_RANK[c.source], crow_mi(origin, (c.lat, c.lon))))
    kept: list[Destination] = []
    for c in pool:
        if all(crow_mi((c.lat, c.lon), (k.lat, k.lon)) > DEDUPE_MI for k in kept):
            kept.append(c)
    return kept


def dest_score(
    dest: Destination, in_season: bool | None, temp_f: float | None, rules: TravelRules
) -> tuple[float, list[str]]:
    """A vault destination scores like its POI. Others start at 1, and campgrounds
    lose a little (paid, less of what the trip is about). Weather counts for all."""
    if dest.poi is not None:
        return score_poi(dest.poi, in_season, temp_f, rules)
    d, why = weather_term(temp_f, rules)
    score, reasons = 1.0 + d, [why] if why else []
    if dest.source == "campground":
        score -= CAMPGROUND_PENALTY
        reasons.insert(0, f"campground fallback (-{CAMPGROUND_PENALTY:g})")
    return score, reasons


@dataclass(frozen=True)
class DestOption:
    dest: Destination
    road_mi: float
    drive_min: float
    arrive: datetime
    """Arrival driving straight through, before any stops."""
    dest_score: float
    dest_reasons: list[str]
    drive: Proposal
    """S2's proposal for this leg: the stops on the way, or why it doesn't fit."""
    total: float


def rank(options: list[DestOption]) -> list[DestOption]:
    return sorted(options, key=lambda o: (0 if o.drive.fits else 1, -o.total, o.road_mi))
