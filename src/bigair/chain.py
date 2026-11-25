"""A rough chain of legs toward a far goal (#92).

Given a route to the goal and a date to be there by, sketch the legs and stays.
It's a frame to improvise against, not an itinerary. Only the next leg is planned
in detail (S3 + S2), and the chain is re-sketched from wherever the van actually
is.

Dan's rules: move every ``stay_nights`` days (3), at most ``max_day_mi`` (300) a
day, a ceiling and never a target. So the chain uses **as many legs as the time
allows** (shorter days), never fewer than the mileage needs, and never shorter
than ``MIN_LEG_MI``. If the date is too close for full stays, stays shorten and
the chain says so. If even 300 mi a day can't make it, the chain is refused
plainly.

Each split point snaps to the best-scoring destination within ``SNAP_MI``
(straight line), ties going to the closest. Where there's none, the leg ends at
an open spot: run "Where next?" from there when the time comes. Leg miles are
approximate: distance along the route between the projected stops, plus each
stop's distance off the route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from bigair.destinations import crow_mi, dest_score
from bigair.poi_near import point_at

if TYPE_CHECKING:
    from datetime import date

    from bigair.destinations import Destination
    from bigair.fuel_sources import RouteInfo
    from bigair.travel_rules import TravelRules

MIN_LEG_MI = 50.0
SNAP_MI = 40.0


@dataclass(frozen=True)
class Leg:
    index: int
    depart: date
    point: tuple[float, float]
    """Where the leg ends: the snapped destination, the open spot, or the goal."""
    to: Destination | None
    is_goal: bool
    approx_mi: float
    nights: int
    """Nights to stay at the end of this leg (0 at the goal)."""


@dataclass(frozen=True)
class Chain:
    legs: tuple[Leg, ...]
    stay_nights: int
    warnings: tuple[str, ...]


def _snap(
    point: tuple[float, float], candidates: list[Destination], rules: TravelRules
) -> Destination | None:
    near = [c for c in candidates if crow_mi(point, (c.lat, c.lon)) <= SNAP_MI]
    if not near:
        return None
    return min(
        near,
        key=lambda c: (-dest_score(c, None, None, rules)[0], crow_mi(point, (c.lat, c.lon))),
    )


def sketch_chain(
    route: RouteInfo,
    start: date,
    by: date,
    candidates: list[Destination],
    rules: TravelRules,
) -> Chain:
    total = route.total_mi
    days = (by - start).days
    min_legs = max(1, math.ceil(total / rules.max_day_mi))
    stay = rules.stay_nights
    n_max = max(0, days) // stay + 1
    warnings: list[str] = []
    if min_legs > n_max:
        n = min_legs
        stay = days // (n - 1) if n > 1 else stay
        if stay < 1:
            msg = (
                f"{total:.0f} mi needs at least {min_legs} days at {rules.max_day_mi:.0f} mi a day;"
                f" there's {max(0, days) + 1}."
            )
            return Chain((), 0, (msg,))
        warnings.append(f"To make the date, stays shorten to {stay} night{'s' * (stay != 1)}.")
    else:
        n = max(min_legs, min(n_max, int(total // MIN_LEG_MI) or 1))

    stops: list[tuple[float, tuple[float, float], Destination | None, float]] = []
    for i in range(1, n):
        mile = total * i / n
        pt = point_at(route, mile)
        snapped = _snap(pt, candidates, rules)
        if snapped is None:
            stops.append((mile, pt, None, 0.0))
        else:
            m, off = route.project(snapped.lat, snapped.lon)
            stops.append((m, (snapped.lat, snapped.lon), snapped, off))

    legs: list[Leg] = []
    prev_mile, prev_off = 0.0, 0.0
    for i, (mile, pt, to, off) in enumerate(stops):
        legs.append(
            Leg(i, start + timedelta(days=i * stay), pt, to, False,
                abs(mile - prev_mile) + prev_off + off, stay)
        )  # fmt: skip
        prev_mile, prev_off = mile, off
    goal = route.points[-1]
    legs.append(
        Leg(n - 1, start + timedelta(days=(n - 1) * stay), goal, None, True,
            abs(total - prev_mile) + prev_off, 0)
    )  # fmt: skip
    for leg in legs:
        if leg.approx_mi > rules.max_day_mi:
            warnings.append(
                f"Leg {leg.index + 1} is about {leg.approx_mi:.0f} mi, over the "
                f"{rules.max_day_mi:.0f} mi ceiling: pick a closer spot."
            )
    return Chain(tuple(legs), stay, tuple(warnings))
