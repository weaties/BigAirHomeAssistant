"""Destination discovery logic (#91): gathering, scoring and ranking. No network."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bigair.day_plan import Proposal
from bigair.destinations import Destination, DestOption, dest_score, gather, rank
from bigair.poi import Poi
from bigair.travel_rules import TravelRules

ORIGIN = (40.0, -111.0)
RULES = TravelRules()


def d(
    name: str, lat: float, lon: float, source: str = "ioverlander", poi: Poi | None = None
) -> Destination:
    return Destination(name, lat, lon, source, kind=None, note=None, poi=poi)


def test_gather_keeps_poi_destinations_and_drops_the_rest_of_the_pois() -> None:
    stay = Poi("Stay Here", 40.5, -111.0, (), None, destination=True)
    visit = Poi("Just Visit", 40.6, -111.0, (), None, destination=False)
    unknown = Poi("Unknown", 40.7, -111.0, (), None)
    out = gather(ORIGIN, [stay, visit, unknown], [], [], RULES)
    assert [(x.name, x.source) for x in out] == [("Stay Here", "poi")]
    assert out[0].poi is stay


def test_gather_drops_candidates_beyond_the_daily_ceiling_as_the_crow_flies() -> None:
    near = d("Near", 41.0, -111.0)  # ~69 mi
    far = d("Far", 45.0, -111.0)  # ~345 mi: can't be ≤300 road miles
    assert [x.name for x in gather(ORIGIN, [], [near, far], [], RULES)] == ["Near"]


def test_gather_drops_candidates_too_close_to_count_as_moving() -> None:
    here = d("Same spot", 40.01, -111.0)
    assert gather(ORIGIN, [], [here], [], RULES) == []


def test_gather_dedupes_nearby_candidates_preferring_the_vault() -> None:
    stay = Poi("Hot Springs", 41.0, -111.0, (), None, destination=True)
    dup = d("Hot Springs camp", 41.005, -111.0)
    land = d("BLM unit", 41.0, -111.01, source="public_land")
    out = gather(ORIGIN, [stay], [dup], [land], RULES)
    assert [x.name for x in out] == ["Hot Springs"]


def test_dest_score_uses_the_poi_score_for_vault_destinations() -> None:
    p = Poi("Stay", 41.0, -111.0, (), None, rating=5, destination=True, best_months=(5,))
    s, reasons = dest_score(d("Stay", 41.0, -111.0, "poi", p), True, 75.0, RULES)
    assert s == pytest.approx(1 + 1.0 + 0.5 + 0.25)
    assert reasons[0] == "★5 (+1.0)"


def test_campgrounds_score_below_dispersed_camping() -> None:
    camp, _ = dest_score(d("KOA", 41, -111, "campground"), None, None, RULES)
    wild, _ = dest_score(d("Pullout", 41, -111, "ioverlander"), None, None, RULES)
    assert camp < wild


def _opt(name: str, total: float, road_mi: float, fits: bool = True) -> DestOption:
    p = Proposal(fits, (), 0.0, 0.0, 0.0, () if fits else ("x",))
    return DestOption(
        dest=d(name, 41, -111),
        road_mi=road_mi,
        drive_min=road_mi * 1.2,
        arrive=datetime(2027, 5, 1, 20, tzinfo=UTC),
        dest_score=total,
        dest_reasons=[],
        drive=p,
        total=total,
    )


def test_rank_best_total_first_closer_wins_ties_and_misfits_last() -> None:
    ranked = rank([_opt("Far tie", 2.0, 250), _opt("Misfit", 9.0, 320, fits=False),
                   _opt("Near tie", 2.0, 120), _opt("Best", 3.0, 280)])  # fmt: skip
    assert [o.dest.name for o in ranked] == ["Best", "Near tie", "Far tie", "Misfit"]
