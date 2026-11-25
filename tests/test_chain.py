"""A rough chain of legs to a far goal (#92). Pure logic, no network."""

from __future__ import annotations

from datetime import date

import pytest

from bigair.chain import sketch_chain
from bigair.destinations import Destination
from bigair.fuel_sources import RouteInfo
from bigair.poi import Poi
from bigair.travel_rules import TravelRules

RULES = TravelRules()  # 300 mi max, 3-night stays
START = date(2027, 5, 1)

# Due north along lon -111: 600 road miles, 12 h.
ROUTE = RouteInfo.from_points([(36.0 + i * 0.4, -111.0) for i in range(21)], 600.0, 12 * 3600.0)


def dest(name: str, lat: float, lon: float = -111.0, source: str = "ioverlander") -> Destination:
    return Destination(name, lat, lon, source, None, None)


def test_more_shorter_legs_fill_the_time_before_the_deadline() -> None:
    # 15 days with 3-night stays: up to 6 legs (5 stays + the last drive). 600 mi / 6 = 100 mi each.
    c = sketch_chain(ROUTE, START, date(2027, 5, 16), [], RULES)
    assert len(c.legs) == 6
    assert c.stay_nights == 3
    assert [leg.depart for leg in c.legs] == [
        date(2027, 5, 1), date(2027, 5, 4), date(2027, 5, 7),
        date(2027, 5, 10), date(2027, 5, 13), date(2027, 5, 16),
    ]  # fmt: skip
    assert all(leg.approx_mi == pytest.approx(100.0, abs=1.0) for leg in c.legs)
    assert c.legs[-1].to is None and c.legs[-1].is_goal
    assert c.warnings == ()


def test_legs_never_get_silly_short() -> None:
    # 60 days for 600 mi would be 21 legs of ~29 mi; stop at MIN_LEG_MI each.
    from bigair.chain import MIN_LEG_MI

    c = sketch_chain(ROUTE, START, date(2027, 6, 30), [], RULES)
    assert len(c.legs) == int(600 // MIN_LEG_MI)


def test_waypoints_snap_to_the_best_nearby_destination() -> None:
    # Split points for 3 legs are at road miles 200 and 400 (lat ~38.67 and ~41.33).
    good = Poi("Lovely Springs", 38.7, -111.1, (), None, rating=5, destination=True)
    candidates = [
        dest("Lovely Springs", 38.7, -111.1, "poi"),
        dest("Plain Pullout", 38.66, -111.0),
        dest("Too Far Off", 38.67, -113.0),
    ]
    candidates[0] = Destination("Lovely Springs", 38.7, -111.1, "poi", None, None, good)
    c = sketch_chain(ROUTE, START, date(2027, 5, 7), candidates, RULES)
    assert len(c.legs) == 3
    assert c.legs[0].to is not None and c.legs[0].to.name == "Lovely Springs"
    assert c.legs[1].to is None and not c.legs[1].is_goal  # nothing near: an open spot
    assert c.legs[1].point == pytest.approx((41.33, -111.0), abs=0.05)


def test_a_tight_deadline_shortens_stays_and_says_so() -> None:
    # 600 mi needs 2 legs at 300 mi. 2 days: the first stay can be only 2 nights.
    c = sketch_chain(ROUTE, START, date(2027, 5, 3), [], RULES)
    assert len(c.legs) == 2
    assert c.stay_nights == 2
    assert c.warnings == ("To make the date, stays shorten to 2 nights.",)


def test_an_impossible_deadline_is_refused_plainly() -> None:
    c = sketch_chain(ROUTE, START, date(2027, 5, 1), [], RULES)
    assert c.legs == ()
    assert c.warnings == ("600 mi needs at least 2 days at 300 mi a day; there's 1.",)


def test_a_short_trip_is_one_leg() -> None:
    short = RouteInfo.from_points([(36.0, -111.0), (37.0, -111.0)], 90.0, 2 * 3600.0)
    c = sketch_chain(short, START, date(2027, 5, 20), [], RULES)
    assert len(c.legs) == 1
    assert c.legs[0].is_goal


def test_a_snap_that_pushes_a_leg_over_the_ceiling_is_flagged() -> None:
    r = RouteInfo.from_points([(36.0 + i * 0.4, -111.0) for i in range(21)], 590.0, 12 * 3600.0)
    # 3 days → 2 legs; the split at mile 295 (lat ~39.93) snaps to a spot ~30 mi off the route.
    off = dest("Off Route Camp", 39.93, -111.57)
    c = sketch_chain(r, START, date(2027, 5, 4), [off], RULES)
    assert len(c.legs) == 2
    assert c.legs[0].to is not None
    # The detour off the route counts on the way in (leg 1) and back out (leg 2).
    assert c.legs[0].approx_mi > 300 and c.legs[1].approx_mi > 300
    assert c.warnings == tuple(
        f"Leg {i} is about {c.legs[i - 1].approx_mi:.0f} mi, over the 300 mi ceiling: "
        "pick a closer spot."
        for i in (1, 2)
    )
