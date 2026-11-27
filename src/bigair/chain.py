"""Slow mode's multi-day plan to a far destination (#117; replaces #92's sketch).

One slow-mode route to the destination is cut into driving days. Each day leaves at
the rules' time (10:00) and must stay under the mileage ceiling (300 mi) **and**
arrive inside the day's window (in by 15:00, before sunset). Those are hard limits.

Every night should be somewhere worth exploring, so the days stretch or shrink to
land on good spots (Dan: "stop short or extend"). A **spot** is a known place to
sleep (a vault destination, an iOverlander site, public land) within ``SNAP_MI``
of the route. A spot is **cool** if it's a vault destination or has things to do
within ``EXPLORE_MI``. Its score is its own (``dest_score``) plus the score of every
vault POI near it (``score_poi``, negatives count as 0).

The whole trip is chosen at once, comparing in this order:

1. the fewest **open** nights (no known spot: find one when the time comes);
2. the fewest nights at a spot that isn't cool;
3. the fewest driving days;
4. the highest total score of the nights.

So a dull night is avoided even at the cost of an extra day, but a cool stop is
never added just to pile up score. Distance never scores: 300 mi is a ceiling,
not a target. A day ends at the goal whenever the goal fits.

Layover days are Dan's per stop: 0 (the default) is one night and drive on in the
morning. The arrival date comes from the layovers; an arrive-by date only warns.

Miles and minutes are estimates. Along the route they use the route's own pace.
A spot's distance off the route counts on the way in and again on the way out,
in straight-line miles at ``OFF_ROUTE_MPH``. Each day's window is taken at the
day's farthest reach. "Plan this day" routes the day for real, and that check is
the one that counts.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
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
    """Where the day ends: the spot, the open spot, or the goal."""
    to: Destination | None
    kind: str
    """``goal``, ``spot`` or ``open``."""
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
    cool: bool = False
    """A vault destination, or things to do nearby."""

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


@dataclass(frozen=True)
class _State:
    cost: tuple[int, int, float]
    """(open nights, dull nights, −score) so far."""
    legs: tuple[Leg, ...]
    mile: float
    off: float


def _day(d: date | datetime) -> str:
    return f"{d:%a %b} {d.day}"


def _explore(
    c: Destination, pois: list[Poi], month: int, rules: TravelRules
) -> tuple[float, list[str], list[str], bool]:
    """(score, reasons, nearby names, cool) for a spot in ``month``."""

    def season(p: Poi) -> bool | None:
        return None if p.best_months is None else month in p.best_months

    total, reasons = dest_score(c, season(c.poi) if c.poi else None, None, rules)
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
    cool = (c.poi is not None and bool(c.poi.destination)) or bool(nearby)
    return total, reasons, nearby, cool


def plan_days(
    route: RouteInfo,
    depart: datetime,
    candidates: list[Destination],
    pois: list[Poi],
    rules: TravelRules,
    layovers: tuple[int, ...] = (),
    by: date | None = None,
) -> Chain:
    total = route.total_mi
    pace = route.duration_s / 60 / total if total > 0 else 0.0
    goal = route.points[-1]
    lay = [max(0, min(MAX_LAYOVER, int(v))) for v in layovers]

    spots = sorted(
        (
            _Placed(d, mile, off)
            for d in candidates
            for mile, off in [route.project(d.lat, d.lon)]
            if off <= SNAP_MI
        ),
        key=lambda p: p.mile,
    )
    miles = [p.mile for p in spots]
    scored: dict[tuple[int, int], tuple[float, list[str], list[str], bool]] = {}

    def explore(i: int, month: int) -> tuple[float, list[str], list[str], bool]:
        if (i, month) not in scored:
            scored[(i, month)] = _explore(spots[i].dest, pois, month, rules)
        return scored[(i, month)]

    def leave(k: int) -> datetime:
        return depart + timedelta(days=k + sum(lay[:k]))

    frontier: dict[tuple[str, float], _State] = {("start", 0.0): _State((0, 0, 0.0), (), 0.0, 0.0)}
    best: tuple[tuple[int, int, int, float], tuple[Leg, ...]] | None = None
    empty_window: str | None = None

    for k in range(MAX_DAYS):
        when = leave(k)
        layover = lay[k] if k < len(lay) else 0
        nxt: dict[tuple[str, float], _State] = {}

        def keep(
            key: tuple[str, float], st: _State, nxt: dict[tuple[str, float], _State] = nxt
        ) -> None:
            old = nxt.get(key)
            if old is None or st.cost < old.cost:
                nxt[key] = st

        for st in frontier.values():
            m0, off0 = st.mile, st.off
            along_goal = total - m0
            gmi = along_goal + off0
            gmin = along_goal * pace + off0 / OFF_ROUTE_MPH * 60
            gb = day_budget(when, rules, goal)
            if gmi <= rules.max_day_mi + _EPS and gmin <= gb.window_min + _EPS:
                leg = Leg(k, when, goal, None, "goal", gmi, gmin, gb.window_min, gb.end_reason, 0)
                done = (st.cost[0], st.cost[1], k + 1, st.cost[2])
                if best is None or done < best[0]:
                    best = (done, (*st.legs, leg))
                continue  # the goal fits: go there

            rb = day_budget(when, rules, point_at(route, min(total, m0 + rules.max_day_mi)))
            window = rb.window_min
            if window <= 0:
                empty_window = (
                    f"No driving time on {_day(when)}: the window from {when:%H:%M} to "
                    f"{rb.end:%H:%M} is empty."
                )
                continue

            lo = bisect_left(miles, m0 + MIN_LEG_MI - _EPS)
            hi = bisect_right(miles, m0 + rules.max_day_mi)
            for i in range(lo, hi):
                p = spots[i]
                mi = p.mile - m0 + off0 + p.off
                mins = (p.mile - m0) * pace + (off0 + p.off) / OFF_ROUTE_MPH * 60
                if mi > rules.max_day_mi + _EPS or mins > window + _EPS:
                    continue
                score, reasons, nearby, cool = explore(i, when.month)
                leg = Leg(
                    k, when, (p.dest.lat, p.dest.lon), p.dest, "spot", mi, mins, window,
                    rb.end_reason, layover, score, tuple(reasons), tuple(nearby), cool,
                )  # fmt: skip
                cost = (st.cost[0], st.cost[1] + (0 if cool else 1), st.cost[2] - score)
                keep(("spot", float(i)), _State(cost, (*st.legs, leg), p.mile, p.off))

            along = min(
                rules.max_day_mi - off0,
                (window - off0 / OFF_ROUTE_MPH * 60) / pace if pace > 0 else total,
                total - m0,
            )
            if along > 0:
                m1 = m0 + along
                mi, mins = along + off0, along * pace + off0 / OFF_ROUTE_MPH * 60
                leg = Leg(k, when, point_at(route, m1), None, "open", mi, mins, window,
                          rb.end_reason, layover)  # fmt: skip
                cost = (st.cost[0] + 1, st.cost[1], st.cost[2])
                keep(("open", round(m1, 3)), _State(cost, (*st.legs, leg), m1, 0.0))

        frontier = nxt
        # Later days only add days; they can still remove dull or open nights.
        if best is not None:
            frontier = {key: st for key, st in frontier.items() if st.cost[:2] < best[0][:2]}
        if not frontier:
            break

    warnings: list[str] = []
    if best is None:
        if empty_window is not None:
            return Chain((), None, (empty_window,))
        return Chain((), None, (f"More than {MAX_DAYS} driving days: check the destination.",))
    legs = best[1]
    last = legs[-1]
    arrive = last.depart + timedelta(minutes=last.drive_min)
    if by is not None and arrive.date() > by:
        warnings.append(f"With these layovers you arrive {_day(arrive)}, after {_day(by)}.")
    return Chain(legs, arrive, tuple(warnings))
