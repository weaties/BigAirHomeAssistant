"""Slow mode (#90): travel rules, the day's time budget, POI scoring and the proposal."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from bigair.day_plan import (
    Candidate,
    day_budget,
    propose,
    score_poi,
    sunset_utc,
)
from bigair.poi import Poi
from bigair.travel_rules import TravelRules, load_rules

if TYPE_CHECKING:
    from pathlib import Path

PDT = timezone(timedelta(hours=-7))
RULES = TravelRules()

# --- rules -------------------------------------------------------------------------


def test_default_rules_are_dans() -> None:
    r = TravelRules()
    assert r.max_day_mi == 300.0
    assert (r.depart_h, r.depart_m) == (10, 0)
    assert (r.arrive_by_h, r.arrive_by_m) == (15, 0)
    assert r.stay_nights == 3
    assert r.avoid_interstates is True
    assert r.no_driving_after_dark is True
    assert r.temp_band_f == (70.0, 80.0)


def test_rules_file_overrides_defaults(tmp_path: Path) -> None:
    f = tmp_path / "rules.yaml"
    f.write_text(
        "max_day_mi: 250\ndepart: '09:30'\narrive_by: '14:00'\n"
        "tag_weights: {hot-springs: 0.5, town: -0.25}\n"
    )
    r = load_rules(f)
    assert r.max_day_mi == 250.0
    assert (r.depart_h, r.depart_m) == (9, 30)
    assert (r.arrive_by_h, r.arrive_by_m) == (14, 0)
    assert r.tag_weights == {"hot-springs": 0.5, "town": -0.25}


def test_missing_rules_file_means_defaults(tmp_path: Path) -> None:
    assert load_rules(tmp_path / "absent.yaml") == TravelRules()
    assert load_rules(None) == TravelRules()


@pytest.mark.parametrize(
    "text",
    [
        "max_day_mi: 0\n",
        "max_day_mi: 900\n",
        "depart: '25:00'\n",
        "arrive_by: noon\n",
        "unknown_key: 1\n",
        "tag_weights: [1, 2]\n",
        "- just a list\n",
    ],
)
def test_bad_rules_file_is_an_error(tmp_path: Path, text: str) -> None:
    f = tmp_path / "rules.yaml"
    f.write_text(text)
    with pytest.raises(ValueError):
        load_rules(f)


# --- sunset ------------------------------------------------------------------------


def test_sunset_seattle_winter_solstice() -> None:
    # 2026-12-21 sunset in Seattle is about 16:20 PST (00:20 UTC on the 22nd).
    s = sunset_utc(47.6062, -122.3321, datetime(2026, 12, 21).date(), -8 * 60)
    assert s is not None
    assert abs((s - datetime(2026, 12, 22, 0, 20, tzinfo=UTC)).total_seconds()) < 5 * 60


def test_sunset_seattle_summer_solstice() -> None:
    # 2026-06-21 sunset in Seattle is about 21:11 PDT (04:11 UTC on the 22nd).
    s = sunset_utc(47.6062, -122.3321, datetime(2026, 6, 21).date(), -7 * 60)
    assert s is not None
    assert abs((s - datetime(2026, 6, 22, 4, 11, tzinfo=UTC)).total_seconds()) < 5 * 60


def test_no_sunset_in_polar_night() -> None:
    assert sunset_utc(71.29, -156.79, datetime(2026, 12, 21).date(), -9 * 60) is None


# --- the day's budget --------------------------------------------------------------


def test_budget_runs_from_departure_to_the_arrive_by_time() -> None:
    depart = datetime(2027, 5, 10, 10, 0, tzinfo=PDT)
    b = day_budget(depart, RULES, (44.0, -121.3))  # central Oregon, sunset ~20:30
    assert b.end == datetime(2027, 5, 10, 15, 0, tzinfo=PDT)
    assert b.end_reason == "arrive_by"
    assert b.window_min == pytest.approx(300.0)


def test_budget_ends_at_sunset_when_that_comes_first() -> None:
    # Fairbanks in December: the sun sets around 14:40 AKST, before 15:00.
    akst = timezone(timedelta(hours=-9))
    depart = datetime(2026, 12, 15, 10, 0, tzinfo=akst)
    b = day_budget(depart, RULES, (64.84, -147.72))
    assert b.end_reason == "sunset"
    assert b.window_min < 300.0


def test_leaving_after_the_arrive_by_time_leaves_no_window() -> None:
    depart = datetime(2027, 5, 10, 16, 0, tzinfo=PDT)
    assert day_budget(depart, RULES, (44.0, -121.3)).window_min == 0.0


# --- scoring -----------------------------------------------------------------------


def _poi(**kw: object) -> Poi:
    base = Poi("P", 41.0, -110.0, (), None)
    return replace(base, **kw)  # type: ignore[arg-type]


def test_score_starts_at_one_for_any_poi() -> None:
    s, reasons = score_poi(_poi(), in_season=None, temp_f=None, rules=RULES)
    assert s == 1.0
    assert reasons == []


def test_score_adds_rating_season_tags_and_weather() -> None:
    rules = replace(RULES, tag_weights={"hot-springs": 0.5})
    p = _poi(rating=5, tags=("hot-springs", "park"))
    s, reasons = score_poi(p, in_season=True, temp_f=74.0, rules=rules)
    # 1 + 0.5*(5-3) + 0.5 (season) + 0.5 (tag) + 0.25 (in the band) = 3.25
    assert s == pytest.approx(3.25)
    assert reasons == ["★5 (+1.0)", "in season (+0.5)", "hot-springs (+0.5)", "74°F at ETA (+0.25)"]


def test_a_three_star_rating_is_neutral_and_not_listed() -> None:
    s, reasons = score_poi(_poi(rating=3), in_season=None, temp_f=None, rules=RULES)
    assert (s, reasons) == (1.0, [])


def test_score_penalises_off_season_low_rating_and_bad_weather() -> None:
    s, reasons = score_poi(_poi(rating=1), in_season=False, temp_f=45.0, rules=RULES)
    # 1 - 1.0 - 0.5 - 0.25 = -0.75
    assert s == pytest.approx(-0.75)
    assert reasons == ["★1 (-1.0)", "off season (-0.5)", "45°F at ETA, outside 70–80°F (-0.25)"]


# --- the proposal ------------------------------------------------------------------


def cand(
    name: str, score: float, detour_min: float, visit_min: float, detour_mi: float = 10.0
) -> Candidate:
    return Candidate(name, score, detour_min, visit_min, detour_mi)


def test_proposal_picks_the_best_scoring_set_that_fits() -> None:
    cands = [
        cand("Springs", 3.0, 40, 120),  # 160 min
        cand("Canyon", 2.5, 80, 90),  # 170 min
        cand("Museum", 1.0, 30, 60),  # 90 min
    ]
    # 300 min window, 120 min driving → 180 min for stops: only one of the big two fits.
    p = propose(cands, drive_min=120, route_mi=150, window_min=300, rules=RULES)
    assert p.fits
    assert p.chosen == ("Springs",)
    assert p.added_min == pytest.approx(160)
    assert p.score == pytest.approx(3.0)


def test_proposal_combines_stops_when_they_fit_together() -> None:
    cands = [cand("A", 1.5, 20, 40), cand("B", 1.5, 20, 40), cand("C", 2.5, 60, 100)]
    p = propose(cands, drive_min=120, route_mi=150, window_min=300, rules=RULES)
    # 180 min free: A+B (120 min, 3.0) beats C alone (160 min, 2.5); C+anything won't fit.
    assert p.chosen == ("A", "B")


def test_proposal_never_picks_a_negative_score() -> None:
    p = propose([cand("Meh", -0.5, 5, 5)], drive_min=60, route_mi=50, window_min=300, rules=RULES)
    assert p.chosen == ()


def test_proposal_respects_the_mileage_ceiling() -> None:
    far = cand("Far", 3.0, 40, 60, detour_mi=80.0)
    p = propose([far], drive_min=200, route_mi=260, window_min=300, rules=RULES)
    assert p.chosen == ()  # 260 + 80 = 340 mi > 300


def test_shorter_day_wins_a_tie_never_a_longer_one() -> None:
    # Equal scores and room for only one: take the one that adds less driving.
    near = cand("Near", 2.0, 20, 60, detour_mi=10)  # 80 min
    far = cand("Far", 2.0, 40, 60, detour_mi=50)  # 100 min
    p = propose([far, near], drive_min=100, route_mi=100, window_min=200, rules=RULES)
    assert p.chosen == ("Near",)


def test_leg_that_does_not_fit_says_so_plainly() -> None:
    p = propose([cand("A", 2.0, 10, 10)], drive_min=400, route_mi=280, window_min=300, rules=RULES)
    assert not p.fits
    assert p.chosen == ()
    assert p.problems == ("6h40 of driving vs a 5h00 window",)


def test_leg_over_the_mileage_ceiling_says_so() -> None:
    p = propose([], drive_min=280, route_mi=340, window_min=300, rules=RULES)
    assert not p.fits
    assert p.problems == ("340 mi is over the 300 mi daily ceiling",)
