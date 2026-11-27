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

# Due north along lon -111: 900 road miles at 60 mph (15 h). 8° of latitude over
# 900 road miles, so road mile m is at lat 36 + m × 8 / 900. In May, sunset is after
# 19:00 everywhere on it, so the 10:00–15:00 window (300 min = 300 mi) binds.
ROUTE = RouteInfo.from_points([(36.0 + i * 0.4, -111.0) for i in range(21)], 900.0, 15 * 3600.0)


def lat_at(mile: float) -> float:
    return 36.0 + mile * 8 / 900


def spot(name: str, mile: float, source: str = "ioverlander", lon: float = -111.0) -> Destination:
    return Destination(name, lat_at(mile), lon, source, None, None)


def walmart(mile: float) -> Destination:
    return Destination(f"Walmart {mile:.0f}", lat_at(mile), -111.0, "walmart", None, None)


def poi(name: str, mile: float, rating: int | None = None, **kw: object) -> Poi:
    return Poi(name, lat_at(mile), -111.05, (), None, rating=rating, **kw)  # type: ignore[arg-type]


def test_with_nothing_known_each_day_ends_at_an_open_spot_as_far_as_the_rules_allow() -> None:
    c = plan_days(ROUTE, DEPART, [], [], [], RULES)
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
    slow = RouteInfo.from_points(list(ROUTE.points), 900.0, 900 / 45 * 3600.0)  # 45 mph
    c = plan_days(slow, DEPART, [], [], [], RULES)
    # 300 min at 45 mph is 225 mi a day: 900 mi takes 4 days.
    assert [round(leg.approx_mi) for leg in c.legs] == [225, 225, 225, 225]
    assert all(leg.drive_min <= leg.window_min + 1e-6 for leg in c.legs)


def test_no_cool_spot_in_reach_means_the_farthest_walmart() -> None:
    plain = spot("Plain Pullout", 250)  # nothing to do nearby: not worth stopping for
    stores = [walmart(150), walmart(280), walmart(310)]  # 310 is past the day's reach
    c = plan_days(ROUTE, DEPART, [plain], stores, [], RULES)
    assert c.legs[0].kind == "walmart"
    assert c.legs[0].to is not None and c.legs[0].to.name == "Walmart 280"


def test_a_day_stops_short_to_reach_somewhere_with_things_to_do() -> None:
    camp = spot("Canyon Camp", 120)
    arch = poi("Big Arch", 125, rating=4)  # within EXPLORE_MI of the camp
    c = plan_days(ROUTE, DEPART, [camp], [walmart(290)], [arch], RULES)
    first = c.legs[0]
    assert first.kind == "spot"
    assert first.to is not None and first.to.name == "Canyon Camp"
    assert first.nearby == ("Big Arch",)
    # The camp scores 1, the arch 1 + ★4 (+0.5).
    assert first.score == pytest.approx(2.5)
    assert round(first.approx_mi) == 120
    # Day 2 starts from the camp; nothing cool ahead, so the Walmart 170 mi on.
    assert c.legs[1].kind == "walmart"
    assert round(c.legs[1].approx_mi) == 170


def test_a_richer_spot_wins_even_if_farther_and_ties_go_to_fewer_miles() -> None:
    near = spot("Near Camp", 100)
    far = spot("Far Camp", 280)
    tie = spot("Tie Camp", 200)
    pois = [poi("Small Thing", 100), poi("Hot Springs", 280, rating=5), poi("Same", 200, rating=5)]
    c = plan_days(ROUTE, DEPART, [near, far, tie], [], pois, RULES)
    # Near: 1 + 1 = 2. Tie and Far: 1 + 2 = 3 each, so the closer one (Tie, 200 mi) wins.
    assert c.legs[0].to is not None and c.legs[0].to.name == "Tie Camp"


def test_distance_alone_never_scores() -> None:
    # Two identical cool spots: the closer one wins (300 mi is a ceiling, not a target).
    a, b = spot("A", 150), spot("B", 290)
    pois = [poi("Near A", 150), poi("Near B", 290)]
    c = plan_days(ROUTE, DEPART, [a, b], [], pois, RULES)
    assert c.legs[0].to is not None and c.legs[0].to.name == "A"


def test_a_vault_destination_is_worth_stopping_for_on_its_own() -> None:
    p = poi("Favourite Camp", 200, rating=5, destination=True)
    d = Destination(p.name, p.lat, p.lon, "poi", None, None, p)
    c = plan_days(ROUTE, DEPART, [d], [walmart(290)], [p], RULES)
    assert c.legs[0].kind == "spot"
    assert c.legs[0].to is not None and c.legs[0].to.name == "Favourite Camp"
    assert c.legs[0].nearby == ()  # it doesn't count itself


def test_spots_are_never_closer_than_the_minimum_leg() -> None:
    camp = spot("Too Soon", MIN_LEG_MI - 10)
    c = plan_days(ROUTE, DEPART, [camp], [], [poi("Thing", MIN_LEG_MI - 10)], RULES)
    assert c.legs[0].kind == "open"


def test_things_to_do_count_only_within_the_explore_radius() -> None:
    camp = spot("Lonely Camp", 200)
    far_thing = poi("Far Thing", 200 + EXPLORE_MI * 2)  # road ≈ crow × 1.6 here, still far
    c = plan_days(ROUTE, DEPART, [camp], [walmart(280)], [far_thing], RULES)
    assert c.legs[0].kind == "walmart"


def test_an_off_season_poor_poi_adds_nothing() -> None:
    camp = spot("Camp", 200)
    meh = poi("Meh", 200, rating=1, best_months=(12,))  # 1 − 1 − 0.5 = −0.5 → 0
    c = plan_days(ROUTE, DEPART, [camp], [walmart(280)], [meh], RULES)
    assert c.legs[0].kind == "walmart"


def test_a_spot_off_the_route_counts_the_spur_both_ways() -> None:
    off = spot("Off Camp", 200, lon=-111.3)  # ~16 mi east-west at lat ~37.8
    c = plan_days(ROUTE, DEPART, [off], [], [poi("Thing", 200)], RULES)
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
    c = plan_days(ROUTE, DEPART, [], [], [], RULES, layovers=(2, 0))
    assert [leg.nights for leg in c.legs] == [3, 1, 0]
    assert [leg.layover for leg in c.legs] == [2, 0, 0]
    assert [leg.depart.date() for leg in c.legs] == [
        date(2027, 5, 1), date(2027, 5, 4), date(2027, 5, 5)
    ]  # fmt: skip
    assert c.arrive.date() == date(2027, 5, 5)


def test_layovers_are_clamped_to_a_sane_range() -> None:
    c = plan_days(ROUTE, DEPART, [], [], [], RULES, layovers=(-3, 999))
    assert [leg.layover for leg in c.legs] == [0, 30, 0]


def test_arriving_after_the_by_date_is_a_warning_not_a_refusal() -> None:
    c = plan_days(ROUTE, DEPART, [], [], [], RULES, layovers=(2, 0), by=date(2027, 5, 4))
    assert len(c.legs) == 3
    assert c.warnings == ("With these layovers you arrive Wed May 5, after Tue May 4.",)


def test_a_goal_in_reach_today_is_one_leg() -> None:
    short = RouteInfo.from_points([(36.0, -111.0), (37.0, -111.0)], 90.0, 1.5 * 3600.0)
    c = plan_days(short, DEPART, [spot("Camp", 40)], [], [poi("Thing", 40)], RULES)
    assert len(c.legs) == 1
    assert c.legs[0].kind == "goal"
    assert c.legs[0].nights == 0


def test_no_driving_window_is_refused_plainly() -> None:
    rules = TravelRules(arrive_by_h=10)  # leave at 10:00, in by 10:00
    c = plan_days(ROUTE, DEPART, [], [], [], rules)
    assert c.legs == ()
    assert c.warnings == ("No driving time on Sat May 1: the window from 10:00 to 10:00 is empty.",)
