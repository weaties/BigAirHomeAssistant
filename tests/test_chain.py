"""Slow mode's multi-day plan to a far destination (#117, replacing #92's sketch).

Pure logic, no network.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from bigair.chain import EXPLORE_MI, MIN_LEG_MI, plan_days
from bigair.destinations import Destination
from bigair.fuel_sources import RouteInfo
from bigair.poi import Poi
from bigair.travel_rules import TravelRules

RULES = TravelRules()  # ≤300 mi, leave 10:00, in by 15:00, before dark
PDT = timezone(timedelta(hours=-7))
DEPART = datetime(2027, 5, 1, 10, 0, tzinfo=PDT)
POINTS = [(36.0 + i * 0.4, -111.0) for i in range(21)]


def north(miles: float, mph: float = 60.0) -> RouteInfo:
    """Due north along lon -111 over 8° of latitude, ``miles`` road miles long. In May,
    sunset is after 19:00 everywhere on it, so at 60 mph the 10:00–15:00 window
    (300 min) and the 300 mi ceiling bind together."""
    return RouteInfo.from_points(POINTS, miles, miles / mph * 3600.0)


ROUTE = north(900.0)


def lat_at(mile: float, route: RouteInfo = ROUTE) -> float:
    return 36.0 + mile * 8 / route.total_mi


def spot(name: str, mile: float, route: RouteInfo = ROUTE, lon: float = -111.0) -> Destination:
    return Destination(name, lat_at(mile, route), lon, "ioverlander", None, None)


def poi(name: str, mile: float, route: RouteInfo = ROUTE, rating: int | None = None,
        **kw: object) -> Poi:  # fmt: skip
    return Poi(name, lat_at(mile, route), -111.05, (), None, rating=rating, **kw)  # type: ignore[arg-type]


def cool(name: str, mile: float, route: RouteInfo = ROUTE, rating: int | None = None) -> tuple[
    Destination, Poi
]:  # fmt: skip
    """A campsite with something to do next to it."""
    return spot(name, mile, route), poi(f"{name} sight", mile, route, rating)


def names(c: object) -> list[str]:
    return [leg.to.name if leg.to else leg.kind for leg in c.legs]  # type: ignore[attr-defined]


def test_with_nothing_known_each_day_ends_at_an_open_spot_as_far_as_the_rules_allow() -> None:
    c = plan_days(ROUTE, DEPART, [], [], RULES)
    assert [leg.kind for leg in c.legs] == ["open", "open", "goal"]
    assert [round(leg.approx_mi) for leg in c.legs] == [300, 300, 300]
    assert [leg.depart.date() for leg in c.legs] == [
        date(2027, 5, 1), date(2027, 5, 2), date(2027, 5, 3)
    ]  # fmt: skip
    assert [leg.nights for leg in c.legs] == [1, 1, 0]
    assert c.legs[0].point == pytest.approx((lat_at(300), -111.0), abs=0.01)
    assert c.arrive == datetime(2027, 5, 3, 15, 0, tzinfo=PDT)
    assert c.warnings == ()


def test_the_3pm_window_binds_before_300_miles_on_slow_roads() -> None:
    c = plan_days(north(900.0, mph=45.0), DEPART, [], [], RULES)
    # 300 min at 45 mph is 225 mi a day: 900 mi takes 4 days.
    assert [round(leg.approx_mi) for leg in c.legs] == [225, 225, 225, 225]
    assert all(leg.drive_min <= leg.window_min + 1e-6 for leg in c.legs)


def test_day_one_stops_short_so_every_night_is_somewhere_cool() -> None:
    r = north(700.0)
    (a, pa), (b, pb) = cool("A", 150, r), cool("B", 420, r)
    plain = spot("Plain", 300, r)  # 300 mi in, but nothing to do there
    c = plan_days(r, DEPART, [a, plain, b], [pa, pb], RULES)
    # Plain at 300 would leave 400 mi: another night somewhere. A → B → goal is all cool.
    assert names(c) == ["A", "B", "goal"]
    assert round(c.legs[0].approx_mi) == 150
    assert all(leg.cool for leg in c.legs[:-1])


def test_an_extra_day_is_worth_it_to_avoid_a_dull_night() -> None:
    r = north(600.0)
    (a, pa), (b, pb) = cool("A", 200, r), cool("B", 400, r)
    plain = spot("Plain", 300, r)
    c = plan_days(r, DEPART, [a, plain, b], [pa, pb], RULES)
    # Plain → goal is 2 days with a dull night; A → B → goal is 3 days, all cool.
    assert names(c) == ["A", "B", "goal"]


def test_fewer_days_beat_a_higher_score() -> None:
    r = north(800.0)
    (x, px) = cool("X", 100, r, rating=5)  # the best spot, but it costs a day
    (y, py), (z, pz) = cool("Y", 250, r), cool("Z", 520, r)
    c = plan_days(r, DEPART, [x, y, z], [px, py, pz], RULES)
    # Y → Z → goal is 3 days. X → Y → Z → goal scores more but takes 4.
    assert names(c) == ["Y", "Z", "goal"]


def test_between_equal_plans_the_richer_nights_win() -> None:
    r = north(560.0)
    (a, pa) = cool("A", 260, r)
    (b, pb) = cool("B", 295, r, rating=5)  # ~35 mi apart: neither counts the other's sight
    c = plan_days(r, DEPART, [a, b], [pa, pb], RULES)
    # Both make it in 2 days with a cool night; B's sight is ★5.
    assert names(c) == ["B", "goal"]
    assert c.legs[0].score == pytest.approx(3.0)  # camp 1 + sight 1 + ★5 (+1)


def test_distance_alone_never_scores() -> None:
    r = north(500.0)
    (a, pa), (b, pb) = cool("A", 220, r), cool("B", 290, r)
    c = plan_days(r, DEPART, [a, b], [pa, pb], RULES)
    # Identical spots, both reach the goal next day: a tie, and the farther one
    # doesn't win it for being farther.
    assert names(c) == ["A", "goal"]


def test_with_nothing_cool_a_known_spot_beats_an_open_one() -> None:
    r = north(560.0)
    c = plan_days(r, DEPART, [spot("Plain", 280, r)], [], RULES)
    assert names(c) == ["Plain", "goal"]
    assert c.legs[0].kind == "spot" and not c.legs[0].cool


def test_a_vault_destination_is_cool_on_its_own() -> None:
    r = north(560.0)
    p = poi("Favourite Camp", 270, r, rating=5, destination=True)
    d = Destination(p.name, p.lat, p.lon, "poi", None, None, p)
    # Plain is ~30 mi away, outside the explore radius of the camp.
    c = plan_days(r, DEPART, [d, spot("Plain", 300, r)], [p], RULES)
    assert names(c) == ["Favourite Camp", "goal"]
    assert c.legs[0].cool
    assert c.legs[0].nearby == ()  # it doesn't count itself


def test_spots_are_never_closer_than_the_minimum_leg() -> None:
    r = north(300.0 + MIN_LEG_MI - 10)
    (a, pa) = cool("Too Soon", MIN_LEG_MI - 10, r)
    c = plan_days(r, DEPART, [a], [pa], RULES)
    assert names(c) == ["open", "goal"]


def test_things_to_do_count_only_within_the_explore_radius() -> None:
    camp = spot("Lonely Camp", 200)
    far_thing = poi("Far Thing", 200 + EXPLORE_MI * 2)  # road ≈ crow × 1.6 here, still far
    c = plan_days(north(500.0), DEPART, [camp], [far_thing], RULES)
    assert not c.legs[0].cool


def test_an_off_season_poor_poi_adds_nothing() -> None:
    r = north(500.0)
    camp = spot("Camp", 250, r)
    meh = poi("Meh", 250, r, rating=1, best_months=(12,))  # 1 − 1 − 0.5 = −0.5 → 0
    c = plan_days(r, DEPART, [camp], [meh], RULES)
    assert c.legs[0].to is not None and not c.legs[0].cool


def test_a_spot_off_the_route_counts_the_spur_both_ways() -> None:
    r = north(700.0)
    off = spot("Off Camp", 200, r, lon=-111.3)  # ~16 mi east-west
    # Off Camp → open → goal has one open night; open → open → goal has two.
    c = plan_days(r, DEPART, [off], [poi("Thing", 200, r)], RULES)
    first, second = c.legs[0], c.legs[1]
    assert first.to is not None and first.to.name == "Off Camp"
    spur = first.approx_mi - 200
    assert 10 < spur < 25
    # Day 2 drives back out to the route first. The spur is slower than the route,
    # so the 3 pm window ends the day short of 300 mi.
    assert second.kind == "open"
    assert second.drive_min == pytest.approx(300, abs=0.5)
    assert second.approx_mi < 300 - 5


def test_layovers_add_days_at_a_stop_and_move_the_arrival() -> None:
    c = plan_days(ROUTE, DEPART, [], [], RULES, layovers=(2, 0))
    assert [leg.nights for leg in c.legs] == [3, 1, 0]
    assert [leg.layover for leg in c.legs] == [2, 0, 0]
    assert [leg.depart.date() for leg in c.legs] == [
        date(2027, 5, 1), date(2027, 5, 4), date(2027, 5, 5)
    ]  # fmt: skip
    assert c.arrive is not None and c.arrive.date() == date(2027, 5, 5)


def test_layovers_are_clamped_to_a_sane_range() -> None:
    c = plan_days(ROUTE, DEPART, [], [], RULES, layovers=(-3, 999))
    assert [leg.layover for leg in c.legs] == [0, 30, 0]


def test_arriving_after_the_by_date_is_a_warning_not_a_refusal() -> None:
    c = plan_days(ROUTE, DEPART, [], [], RULES, layovers=(2, 0), by=date(2027, 5, 4))
    assert len(c.legs) == 3
    assert c.warnings == ("With these layovers you arrive Wed May 5, after Tue May 4.",)


def test_a_goal_in_reach_today_is_one_leg() -> None:
    short = RouteInfo.from_points([(36.0, -111.0), (37.0, -111.0)], 90.0, 1.5 * 3600.0)
    camp = Destination("Camp", 36.4, -111.0, "ioverlander", None, None)
    c = plan_days(short, DEPART, [camp], [Poi("Thing", 36.4, -111.0, (), None)], RULES)
    assert names(c) == ["goal"]
    assert c.legs[0].nights == 0


def test_no_driving_window_is_refused_plainly() -> None:
    rules = TravelRules(arrive_by_h=10)  # leave at 10:00, in by 10:00
    c = plan_days(ROUTE, DEPART, [], [], rules)
    assert c.legs == ()
    assert c.warnings == ("No driving time on Sat May 1: the window from 10:00 to 10:00 is empty.",)


def test_many_spots_plan_quickly() -> None:
    import time

    r = north(1500.0)
    spots = [spot(f"S{i}", i * 3.0, r) for i in range(1, 500)]
    pois = [poi(f"P{i}", i * 30.0, r) for i in range(1, 50)]
    t = time.perf_counter()
    c = plan_days(r, DEPART, spots, pois, RULES)
    assert time.perf_counter() - t < 5.0
    assert c.legs[-1].is_goal
    assert all(leg.cool for leg in c.legs[:-1])


def test_nearby_pois_carry_their_note_files() -> None:
    r = north(500.0)
    camp = spot("Camp", 250, r)
    sight = Poi("Sight", lat_at(250, r), -111.05, (), None, file="Sight.md")
    c = plan_days(r, DEPART, [camp], [sight], RULES)
    assert c.legs[0].nearby == ("Sight",)
    assert c.legs[0].nearby_files == ("Sight.md",)
