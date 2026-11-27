"""Fuel-stop planner (#70) — hand-solved synthetic routes.

Numbers are chosen so every leg is a whole number of gallons, so the 0.5-gal
grid introduces no rounding and each answer can be checked by hand.
"""

from __future__ import annotations

import pytest

from bigair.fuel_plan import (
    PlanInput,
    PlanRefusedError,
    Station,
    format_options,
    naive_plan,
    plan,
    plan_options,
)


def _inp(**kw: object) -> PlanInput:
    base: dict[str, object] = {
        "route_mi": 300.0,
        "stations": (),
        "start_gal": 10.0,
        "mpg": 10.0,
        "capacity_gal": 20.0,
        "reserve_mi": 0.0,
        "stop_penalty_usd": 0.0,
        "end_value_per_gal": 0.0,
        "min_buy_gal": 0.5,
    }
    base.update(kw)
    return PlanInput(**base)  # type: ignore[arg-type]


A = Station("A", mile=50.0, price_per_gal=4.0)
B = Station("B", mile=150.0, price_per_gal=3.0)


def test_buys_just_enough_at_dear_station_to_reach_cheap_one() -> None:
    # Arrive A with 5; buy 5 ($20) to reach B empty; buy 15 at B ($45) to finish.
    p = plan(_inp(stations=(A, B)))
    assert p.feasible
    assert [(s.station.name, s.buy_gal) for s in p.stops] == [("A", 5.0), ("B", 15.0)]
    assert p.fuel_cost_usd == pytest.approx(65.0)
    assert p.end_gal == pytest.approx(0.0)


def test_reserve_is_never_entered() -> None:
    # 20 mi reserve = 2 gal. A: arrive 5, buy 7 ($28). B: arrive 2, buy 15 ($45). End with 2.
    p = plan(_inp(stations=(A, B), reserve_mi=20.0))
    assert [(s.station.name, s.buy_gal) for s in p.stops] == [("A", 7.0), ("B", 15.0)]
    assert p.fuel_cost_usd == pytest.approx(73.0)
    assert p.min_gal >= 2.0
    assert p.end_gal == pytest.approx(2.0)


def test_leftover_fuel_value_makes_it_fill_up_cheap() -> None:
    s = Station("S", mile=40.0, price_per_gal=3.0)
    lean = plan(_inp(route_mi=100.0, start_gal=5.0, stations=(s,)))
    assert lean.stops[0].buy_gal == pytest.approx(5.0)  # arrive 1, need 6 → buy 5
    full = plan(_inp(route_mi=100.0, start_gal=5.0, stations=(s,), end_value_per_gal=5.0))
    assert full.stops[0].buy_gal == pytest.approx(19.0)  # fill to 20, home fuel worth more
    assert full.end_gal == pytest.approx(14.0)
    assert full.net_cost_usd == pytest.approx(19 * 3.0 - 14 * 5.0)


def test_stop_penalty_avoids_a_pointless_extra_stop() -> None:
    # Start 10 reaches C (mile 100) exactly. D (mile 50) is 10c cheaper than C.
    c = Station("C", mile=100.0, price_per_gal=4.0)
    d = Station("D", mile=50.0, price_per_gal=3.9)
    inp = _inp(route_mi=200.0, stations=(c, d), capacity_gal=30.0)
    # No penalty: buy everything (15 gal) at D, the cheaper one.
    assert [s.station.name for s in plan(inp).stops] == ["D"]
    # $10/stop doesn't change a one-stop plan.
    p = plan(_inp(route_mi=200.0, stations=(c, d), capacity_gal=30.0, stop_penalty_usd=10.0))
    assert len(p.stops) == 1


def test_detour_costs_fuel_and_time() -> None:
    near = Station("near", mile=100.0, price_per_gal=3.0)
    far = Station("far", mile=100.0, price_per_gal=2.9, detour_mi=20.0, detour_min=30.0)
    # near: arrive 5, buy 5 = $25. far: buy 6 (2 gal detour) = $17.40 + 30 min at $20/h = $27.40.
    p = plan(_inp(route_mi=200.0, start_gal=15.0, stations=(near, far), time_value_usd_per_hr=20.0))
    assert [s.station.name for s in p.stops] == ["near"]


def test_infeasible_gap_is_reported_not_guessed() -> None:
    # 20-gal tank at 10 mpg = 200 mi; stations 250 mi apart.
    p = plan(_inp(route_mi=400.0, stations=(Station("X", 100.0, 3.0),), start_gal=10.0))
    assert not p.feasible
    assert p.reason


def test_start_below_reserve_is_infeasible() -> None:
    p = plan(_inp(start_gal=1.0, reserve_mi=20.0, stations=(A, B)))
    assert not p.feasible
    assert "reserve" in p.reason


def test_stale_fuel_data_is_refused() -> None:
    with pytest.raises(PlanRefusedError):
        plan(_inp(stations=(A, B), stale=True))


def test_calibrating_plans_with_a_lower_mpg() -> None:
    inp = _inp(stations=(A, B), calibrating=True, calibrating_mpg_factor=0.8)
    assert inp.planning_mpg == pytest.approx(8.0)
    p = plan(inp)
    # 300 mi at 8 mpg burns ≥ 37.5 gal (legs round up on the grid) vs 30 at 10 mpg.
    assert p.feasible
    assert p.fuel_bought_gal - p.end_gal >= 37.5 - 10.0


def test_non_diesel_and_off_route_stations_ignored() -> None:
    gas = Station("gas", mile=150.0, price_per_gal=1.0, diesel=False)
    beyond = Station("beyond", mile=900.0, price_per_gal=1.0)
    p = plan(_inp(stations=(A, B, gas, beyond)))
    assert {s.station.name for s in p.stops} == {"A", "B"}


def test_fractional_start_is_rounded_down() -> None:
    # 10.4 gal starts as 10.0 on the grid: conservative.
    p = plan(_inp(stations=(A, B), start_gal=10.4))
    assert p.stops[0].arrive_gal == pytest.approx(5.0)


def test_min_buy_skips_tiny_top_ups() -> None:
    p = plan(_inp(stations=(A, B), min_buy_gal=6.0))
    assert all(s.buy_gal >= 6.0 for s in p.stops)


def _random_route(seed: int) -> PlanInput:
    import random

    rnd = random.Random(seed)
    stations = tuple(
        Station(f"S{i}", mile=rnd.uniform(0, 1500), price_per_gal=round(rnd.uniform(5, 8), 2))
        for i in range(40)
    )
    return PlanInput(
        route_mi=1500.0,
        stations=stations,
        start_gal=30.0,
        mpg=14.0,
        capacity_gal=47.0,
        reserve_mi=100.0,
        stop_penalty_usd=10.0,
        end_value_per_gal=6.5,
    )


@pytest.mark.parametrize("seed", range(5))
def test_plan_never_dips_below_reserve(seed: int) -> None:
    inp = _random_route(seed)
    p = plan(inp)
    if not p.feasible:
        pytest.skip("random route infeasible")
    reserve_gal = inp.reserve_mi / inp.planning_mpg
    assert p.min_gal >= reserve_gal - 1e-9
    assert all(s.arrive_gal >= reserve_gal - 1e-9 for s in p.stops)
    assert all(s.arrive_gal + s.buy_gal <= inp.capacity_gal + 1e-9 for s in p.stops)


@pytest.mark.parametrize("seed", range(5))
def test_beats_or_matches_naive(seed: int) -> None:
    inp = _random_route(seed)
    p, n = plan(inp), naive_plan(inp)
    # Comparable only when the naive plan also stayed out of the reserve; the
    # slack covers the conservative grid rounding.
    if p.feasible and n.feasible and n.min_gal >= inp.reserve_gal + 0.5:
        assert p.net_cost_usd <= n.net_cost_usd + 2.0


def test_deeper_reserve_never_costs_more() -> None:
    inp = _random_route(1)
    opts = plan_options(inp, (150.0, 100.0, 50.0, 25.0))
    feasible = [o for o in opts if o.feasible]
    assert len(feasible) >= 2
    costs = [o.net_cost_usd for o in feasible]
    assert costs == sorted(costs, reverse=True) or all(
        a >= b - 1e-6 for a, b in zip(costs, costs[1:], strict=False)
    )
    assert [o.reserve_mi for o in opts] == [150.0, 100.0, 50.0, 25.0]


def test_naive_fills_at_quarter_tank() -> None:
    # A: arrive 5 > 5? quarter of 20 = 5 → fill to 20 ($60). End: 20 − 25 → runs out.
    n = naive_plan(_inp(stations=(A, B)))
    assert n.stops[0].station.name == "A"
    assert n.stops[0].buy_gal == pytest.approx(15.0)


def test_format_options_shows_each_reserve_and_savings() -> None:
    inp = _random_route(1)
    text = format_options(inp, plan_options(inp, (150.0, 50.0)), naive_plan(inp))
    assert "150 mi" in text
    assert "50 mi" in text
    assert "fill at 1/4" in text


def test_many_unused_stations_dont_compound_rounding() -> None:
    # Regression: rounding each leg up separately made 40 stations cost gallons.
    dear = tuple(Station(f"D{i}", mile=i * 7.3 + 1.1, price_per_gal=99.0) for i in range(40))
    p = plan(_inp(stations=(A, B, *dear)))
    assert [(s.station.name, s.buy_gal) for s in p.stops] == [("A", 5.0), ("B", 15.0)]
    assert p.fuel_cost_usd == pytest.approx(65.0)


def test_trip_from_dict() -> None:
    from bigair.fuel_plan import trip_from_dict

    route_mi, stations, home = trip_from_dict(
        {
            "route_mi": 300,
            "home_price_per_gal": 6.5,
            "stations": [
                {"name": "A", "mile": 50, "price_per_gal": 4.0},
                {"name": "G", "mile": 60, "price_per_gal": 3.0, "diesel": False, "detour_mi": 1},
            ],
        }
    )
    assert route_mi == 300.0
    assert home == 6.5
    assert stations[0] == A
    assert stations[1].diesel is False
    assert stations[1].detour_mi == 1.0


def test_trip_without_home_price_uses_median_near_the_end() -> None:
    from bigair.fuel_plan import trip_from_dict

    _, _, home = trip_from_dict(
        {
            "route_mi": 300,
            "stations": [
                {"name": "B", "mile": 150, "price_per_gal": 3.0},
                {"name": "A", "mile": 50, "price_per_gal": 4.0},
            ],
        }
    )
    assert home == 3.5  # fewer than 3 within 30 mi: median of the last five


def test_trip_round_trips_through_dict() -> None:
    from bigair.fuel_plan import trip_from_dict, trip_to_dict

    s = Station(
        "Love's, CA", 640.0, 6.49, 0.4, 0.8, True, "Open Roads −$0.30; 3 h old", 39.9, -122.2
    )
    assert trip_from_dict(trip_to_dict(780.0, (s,), 6.89)) == (780.0, (s,), 6.89)


def test_notes_are_printed() -> None:
    s = Station("A", 50.0, 4.0, note="Open Roads −$0.30")
    inp = _inp(stations=(s, B))
    assert "(Open Roads −$0.30)" in format_options(inp, plan_options(inp, (0.0,)))


def test_home_price_ignores_one_expensive_station_at_the_end() -> None:
    from bigair.fuel_plan import default_home_price

    near = (
        Station("cheap1", 280.0, 6.20),
        Station("cheap2", 285.0, 6.30),
        Station("cheap3", 290.0, 6.40),
        Station("pricey", 299.0, 7.70),
        Station("far", 10.0, 9.99),
    )
    assert default_home_price(near, 300.0) == pytest.approx(6.35)
