"""Slow mode (#90): the day's time budget, POI scoring, and the proposed stops.

The **budget** runs from departure to the earlier of the arrive-by time (15:00)
and sunset at the destination (no driving after dark), in the departure's own
UTC offset.

The **score** is deliberately simple, so it can be explained line by line:

    1                                  any POI is worth something
    + 0.5 × (rating − 3)               Dan's ★1–5 (★5 = +1, ★1 = −1)
    ± 0.5                              in / out of its best months
    + Σ tag weights                    from the travel rules
    ± 0.25                             forecast at the ETA inside / outside 70–80°F

Distance is not in the score. A longer day never scores higher (Dan: 300 mi is
a ceiling, never a target).

The **proposal** is the set of POIs with the highest total score whose detours
and visits fit in the time left after the drive, and whose detour miles keep the
day under the mileage ceiling. Ties go to the set that adds less time, then fewer
miles. Only POIs with a known detour and a known visit time are candidates. The
rest are listed but never guessed. If the drive alone doesn't fit, the proposal
is empty and says why.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bigair.poi import Poi
    from bigair.travel_rules import TravelRules

MAX_CANDIDATES = 15
"""Top-scoring candidates searched exhaustively (2^15 subsets at most)."""


def hm(minutes: float) -> str:
    m = max(0, round(minutes))
    return f"{m // 60}h{m % 60:02d}"


# --- sunset (NOAA solar calculator equations) ---------------------------------------


def _cos_hour_angle(lat: float, lon: float, d: date, offset_min: int) -> tuple[float, float]:
    """(cos of the sunset hour angle, solar noon in minutes after 00:00 UTC of ``d``)."""
    jd = d.toordinal() + 1721424.5 + (720 - offset_min) / 1440
    t = (jd - 2451545.0) / 36525
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (
        math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
        + math.sin(3 * mr) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * t)
    lam = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(eps) * math.sin(lam))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eqt = 4 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * mr)
    )
    latr = math.radians(lat)
    cos_h = math.cos(math.radians(90.833)) / (math.cos(latr) * math.cos(decl)) - math.tan(
        latr
    ) * math.tan(decl)
    return cos_h, 720 - 4 * lon - eqt


def sunset_utc(lat: float, lon: float, d: date, offset_min: int) -> datetime | None:
    """Sunset on local date ``d`` (UTC offset ``offset_min``), or None if the sun
    doesn't set or rise that day."""
    cos_h, noon = _cos_hour_angle(lat, lon, d, offset_min)
    if not -1 <= cos_h <= 1:
        return None
    minutes = noon + 4 * math.degrees(math.acos(cos_h))
    t = datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(minutes=minutes)
    return t.replace(microsecond=0)


# --- the day's budget ---------------------------------------------------------------


@dataclass(frozen=True)
class DayBudget:
    depart: datetime
    arrive_by: datetime
    sunset: datetime | None
    end: datetime
    end_reason: str
    """``arrive_by``, ``sunset`` or ``polar_night``."""
    window_min: float


def day_budget(depart: datetime, rules: TravelRules, dest: tuple[float, float]) -> DayBudget:
    offset = depart.utcoffset() or timedelta(0)
    offset_min = round(offset.total_seconds() / 60)
    arrive_by = depart.replace(hour=rules.arrive_by_h, minute=rules.arrive_by_m, second=0)
    arrive_by = arrive_by.replace(microsecond=0)
    local_day = depart.date()
    sunset = sunset_utc(dest[0], dest[1], local_day, offset_min)
    end, reason = arrive_by, "arrive_by"
    if rules.no_driving_after_dark:
        cos_h, _ = _cos_hour_angle(dest[0], dest[1], local_day, offset_min)
        if cos_h > 1:  # the sun never rises
            end, reason = depart, "polar_night"
        elif sunset is not None and sunset < arrive_by:
            end, reason = sunset.astimezone(depart.tzinfo), "sunset"
    window = max(0.0, (end - depart).total_seconds() / 60)
    return DayBudget(depart, arrive_by, sunset, end, reason, window)


# --- scoring ------------------------------------------------------------------------


def _signed(x: float) -> str:
    return f"+{x:g}" if x >= 0 else f"{x:g}"


def score_poi(
    poi: Poi, in_season: bool | None, temp_f: float | None, rules: TravelRules
) -> tuple[float, list[str]]:
    """The POI's score and the reasons, one per term that isn't zero."""
    score = 1.0
    reasons: list[str] = []
    if poi.rating is not None and poi.rating != 3:
        d = 0.5 * (poi.rating - 3)
        score += d
        reasons.append(f"★{poi.rating} ({d:+.1f})")
    if in_season is True:
        score += 0.5
        reasons.append("in season (+0.5)")
    elif in_season is False:
        score -= 0.5
        reasons.append("off season (-0.5)")
    for tag in poi.tags:
        w = rules.tag_weights.get(tag)
        if w:
            score += w
            reasons.append(f"{tag} ({_signed(w)})")
    d, why = weather_term(temp_f, rules)
    if why:
        score += d
        reasons.append(why)
    return score, reasons


def weather_term(temp_f: float | None, rules: TravelRules) -> tuple[float, str | None]:
    """±0.25 for the forecast at the ETA inside / outside the band; 0 if unknown."""
    if temp_f is None:
        return 0.0, None
    lo, hi = rules.temp_band_f
    if lo <= temp_f <= hi:
        return 0.25, f"{temp_f:.0f}°F at ETA (+0.25)"
    return -0.25, f"{temp_f:.0f}°F at ETA, outside {lo:g}–{hi:g}°F (-0.25)"


# --- the proposal -------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    name: str
    score: float
    detour_min: float
    visit_min: float
    detour_mi: float

    @property
    def added_min(self) -> float:
        return self.detour_min + self.visit_min


@dataclass(frozen=True)
class Proposal:
    fits: bool
    chosen: tuple[str, ...]
    added_min: float
    added_mi: float
    score: float
    problems: tuple[str, ...]


def propose(
    cands: list[Candidate],
    drive_min: float,
    route_mi: float,
    window_min: float,
    rules: TravelRules,
) -> Proposal:
    problems: list[str] = []
    if drive_min > window_min:
        problems.append(f"{hm(drive_min)} of driving vs a {hm(window_min)} window")
    if route_mi > rules.max_day_mi:
        problems.append(f"{route_mi:.0f} mi is over the {rules.max_day_mi:.0f} mi daily ceiling")
    if problems:
        return Proposal(False, (), 0.0, 0.0, 0.0, tuple(problems))

    free_min = window_min - drive_min
    free_mi = rules.max_day_mi - route_mi
    pool = [c for c in cands if c.score > 0 and c.added_min <= free_min and c.detour_mi <= free_mi]
    pool = sorted(pool, key=lambda c: -c.score)[:MAX_CANDIDATES]
    order = {c.name: i for i, c in enumerate(cands)}

    best: tuple[float, float, float] = (0.0, 0.0, 0.0)  # score, -minutes, -miles
    best_set: tuple[Candidate, ...] = ()
    for k in range(1, len(pool) + 1):
        for combo in combinations(pool, k):
            mins = sum(c.added_min for c in combo)
            mi = sum(c.detour_mi for c in combo)
            if mins > free_min or mi > free_mi:
                continue
            key = (round(sum(c.score for c in combo), 9), -mins, -mi)
            if key > best:
                best, best_set = key, combo
    chosen = tuple(sorted((c.name for c in best_set), key=lambda n: order[n]))
    return Proposal(True, chosen, -best[1], -best[2], best[0], ())
