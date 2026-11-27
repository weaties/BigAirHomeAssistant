"""Slow mode's multi-day plan to a far destination (#117; replaces #92's sketch).

One slow-mode route to the destination is cut into driving days. Each day leaves at
the rules' time (10:00) and must stay under the mileage ceiling (300 mi) and arrive
inside the day's window (in by 15:00, before sunset). Where each day ends is
**scored**, so a day can stop short or run long to reach somewhere worth
exploring:

1. **The goal**, if it fits today.
2. Otherwise the **best spot** in reach: a known place to sleep (a vault
   destination, an iOverlander site, public land) within ``SNAP_MI`` of the route
   and at least ``MIN_LEG_MI`` along. A spot counts only if it's worth stopping
   for: a vault destination, or one with things to do within ``EXPLORE_MI``. Its
   score is its own (``dest_score``) plus the score of every vault POI near it
   (``score_poi``, negatives count as 0). Ties go to fewer miles. **Distance never
   scores**: 300 mi is a ceiling, not a target.
3. Otherwise the **farthest Walmart** in reach (Dan: "stop at 300 at a Walmart").
4. Otherwise an **open spot** as far along as the rules allow: find a spot there
   when the time comes.

Layover days are Dan's per stop: 0 (the default) is one night and drive on in the
morning. The arrival date comes from the layovers; an arrive-by date only warns.

Miles and minutes are estimates. Along the route they use the route's own pace.
A spot's distance off the route counts on the way in and again on the way out,
in straight-line miles at ``OFF_ROUTE_MPH``. "Plan this day" routes the day for
real, and that check is the one that counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from bigair.day_plan import day_budget, score_poi
from bigair.destinations import crow_mi, dest_score
from bigair.poi_near import point_at

if TYPE_CHECKING:
    from datetime import date, datetime

    from bigair.destinations import Destination
    from bigair.fuel_sources import RouteInfo
    from bigair.poi import Poi
    from bigair.travel_rules import TravelRules

MIN_LEG_MI = 50.0
SNAP_MI = 40.0
EXPLORE_MI = 25.0
"""Straight-line radius for "things to do nearby"."""
OFF_ROUTE_MPH = 35.0
"""Straight-line pace off the route: slower roads, and road miles exceed crow miles."""
MAX_LAYOVER = 30
MAX_DAYS = 60
_EPS = 1e-6


@dataclass(frozen=True)
class Leg:
    index: int
    depart: datetime
    point: tuple[float, float]
    """Where the day ends: the spot, the Walmart, the open spot, or the goal."""
    to: Destination | None
    kind: str
    """``goal``, ``spot``, ``walmart`` or ``open``."""
    approx_mi: float
    drive_min: float
    window_min: float
    end_reason: str
    """What ends the day's window: ``arrive_by``, ``sunset`` or ``polar_night``."""
    layover: int
    """Extra days at this stop (0 at the goal)."""
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    nearby: tuple[str, ...] = ()
    """Vault POIs within ``EXPLORE_MI`` that add to the score."""

    @property
    def nights(self) -> int:
        return 0 if self.kind == "goal" else 1 + self.layover

    @property
    def is_goal(self) -> bool:
        return self.kind == "goal"


@dataclass(frozen=True)
class Chain:
    legs: tuple[Leg, ...]
    arrive: datetime | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Placed:
    dest: Destination
    mile: float
    off: float


def _day(d: date | datetime) -> str:
    return f"{d:%a %b} {d.day}"


def _explore(
    c: Destination, pois: list[Poi], month: int, rules: TravelRules
) -> tuple[float, float, list[str], list[str]]:
    """(own score, total score, reasons, nearby names) for a spot in ``month``."""

    def season(p: Poi) -> bool | None:
        return None if p.best_months is None else month in p.best_months

    own, reasons = dest_score(c, season(c.poi) if c.poi else None, None, rules)
    total = own
    nearby: list[str] = []
    for p in pois:
        if p is c.poi or (p.name == c.name and (p.lat, p.lon) == (c.lat, c.lon)):
            continue
        if crow_mi((c.lat, c.lon), (p.lat, p.lon)) > EXPLORE_MI:
            continue
        s, _ = score_poi(p, season(p), None, rules)
        if s > 0:
            total += s
            nearby.append(p.name)
            reasons.append(f"near {p.name} (+{s:g})")
    return own, total, reasons, nearby


def plan_days(
    route: RouteInfo,
    depart: datetime,
    candidates: list[Destination],
    walmarts: list[Destination],
    pois: list[Poi],
    rules: TravelRules,
    layovers: tuple[int, ...] = (),
    by: date | None = None,
) -> Chain:
    total = route.total_mi
    pace = route.duration_s / 60 / total if total > 0 else 0.0
    goal = route.points[-1]

    def place(ds: list[Destination]) -> list[_Placed]:
        out = []
        for d in ds:
            mile, off = route.project(d.lat, d.lon)
            if off <= SNAP_MI:
                out.append(_Placed(d, mile, off))
        return out

    spots, stores = place(candidates), place(walmarts)
    legs: list[Leg] = []
    warnings: list[str] = []
    m0, off0, when = 0.0, 0.0, depart

    def drive(mile: float, off: float) -> tuple[float, float]:
        along = mile - m0
        return along + off0 + off, along * pace + (off0 + off) / OFF_ROUTE_MPH * 60

    def fits(p: _Placed) -> tuple[bool, float, float, float, str]:
        mi, mins = drive(p.mile, p.off)
        b = day_budget(when, rules, (p.dest.lat, p.dest.lon))
        ok = (
            p.mile - m0 >= MIN_LEG_MI
            and mi <= rules.max_day_mi + _EPS
            and mins <= b.window_min + _EPS
        )
        return ok, mi, mins, b.window_min, b.end_reason

    for i in range(MAX_DAYS):
        layover = 0
        if i < len(layovers):
            layover = max(0, min(MAX_LAYOVER, int(layovers[i])))

        gmi, gmin = drive(total, 0.0)
        gb = day_budget(when, rules, goal)
        if gmi <= rules.max_day_mi + _EPS and gmin <= gb.window_min + _EPS:
            legs.append(
                Leg(i, when, goal, None, "goal", gmi, gmin, gb.window_min, gb.end_reason, 0)
            )
            arrive = when + timedelta(minutes=gmin)
            if by is not None and arrive.date() > by:
                warnings.append(f"With these layovers you arrive {_day(arrive)}, after {_day(by)}.")
            return Chain(tuple(legs), arrive, tuple(warnings))

        reach_pt = point_at(route, min(total, m0 + rules.max_day_mi))
        rb = day_budget(when, rules, reach_pt)
        if rb.window_min <= 0:
            warnings.append(
                f"No driving time on {_day(when)}: the window from {when:%H:%M} to "
                f"{rb.end:%H:%M} is empty."
            )
            return Chain((), None, tuple(warnings))

        month = when.month
        best: tuple[tuple[float, float], Leg, _Placed] | None = None
        for p in spots:
            ok, mi, mins, window, reason = fits(p)
            if not ok:
                continue
            own, score, reasons, nearby = _explore(p.dest, pois, month, rules)
            vault_dest = p.dest.poi is not None and bool(p.dest.poi.destination)
            if not (vault_dest or nearby):
                continue
            leg = Leg(i, when, (p.dest.lat, p.dest.lon), p.dest, "spot", mi, mins, window,
                      reason, layover, score, tuple(reasons), tuple(nearby))  # fmt: skip
            key = (round(score, 9), -mi)
            if best is None or key > best[0]:
                best = (key, leg, p)
        chosen: tuple[Leg, float, float] | None = None
        if best is not None:
            chosen = (best[1], best[2].mile, best[2].off)
        else:
            far: tuple[float, Leg, _Placed] | None = None
            for p in stores:
                ok, mi, mins, window, reason = fits(p)
                if ok and (far is None or p.mile > far[0]):
                    leg = Leg(i, when, (p.dest.lat, p.dest.lon), p.dest, "walmart", mi, mins,
                              window, reason, layover)  # fmt: skip
                    far = (p.mile, leg, p)
            if far is not None:
                chosen = (far[1], far[2].mile, far[2].off)
        if chosen is None:
            along = min(
                rules.max_day_mi - off0,
                (rb.window_min - off0 / OFF_ROUTE_MPH * 60) / pace if pace > 0 else total,
                total - m0,
            )
            if along <= 0:
                warnings.append(
                    f"Day {i + 1} can't move: the drive back to the route uses the whole day."
                )
                return Chain(tuple(legs), None, tuple(warnings))
            mi, mins = drive(m0 + along, 0.0)
            pt = point_at(route, m0 + along)
            leg = Leg(i, when, pt, None, "open", mi, mins, rb.window_min, rb.end_reason, layover)
            chosen = (leg, m0 + along, 0.0)

        leg, m0, off0 = chosen
        legs.append(leg)
        when = when + timedelta(days=leg.nights)

    warnings.append(f"More than {MAX_DAYS} driving days: check the destination.")
    return Chain(tuple(legs), None, tuple(warnings))
