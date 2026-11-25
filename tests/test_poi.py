"""POI notes from the vault → Poi (#88). Synthetic notes only: the real ones are private."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bigair.poi import Poi, load_pois, parse_poi

if TYPE_CHECKING:
    from pathlib import Path

FULL = """---
location: "38.5000, -111.2000"
tags: [trip-2027, rt27-c, geology]
visit_min: 90
visit_what: "Canyon rim walk + overlook"
best_months: [4, 5, 9, 10]
destination: true
rating: 4
---
# Example Canyon - Rimtown

> A made-up canyon with a long rim road.

**At a glance** — Phase C · Apr–May, Sep–Oct.
"""

BARE = """---
location: "36.1, -115.2"
tags: [trip-2027, park]
---
Body with no heading.
"""


def test_full_note_parses_every_field() -> None:
    p = parse_poi(FULL, "Example Canyon - Rimtown.md")
    assert p == Poi(
        name="Example Canyon - Rimtown",
        lat=38.5,
        lon=-111.2,
        tags=("trip-2027", "rt27-c", "geology"),
        summary="A made-up canyon with a long rim road.",
        visit_min=90,
        visit_what="Canyon rim walk + overlook",
        best_months=(4, 5, 9, 10),
        destination=True,
        rating=4,
    )


def test_missing_planning_fields_stay_none_never_guessed() -> None:
    p = parse_poi(BARE, "Bare Place.md")
    assert p is not None
    assert p.name == "Bare Place"  # no H1 → file name
    assert p.summary is None
    assert p.visit_min is None
    assert p.visit_what is None
    assert p.best_months is None
    assert p.destination is None
    assert p.rating is None


def test_note_without_location_is_not_a_poi() -> None:
    assert parse_poi("---\ntags: [x]\n---\n# Nowhere\n", "Nowhere.md") is None


def test_note_without_frontmatter_is_not_a_poi() -> None:
    assert parse_poi("# Just text\n", "Text.md") is None


def test_malformed_yaml_is_not_a_poi() -> None:
    assert parse_poi('---\nlocation: "1, 2\ntags: [\n---\n', "Broken.md") is None


def test_out_of_range_location_is_not_a_poi() -> None:
    assert parse_poi('---\nlocation: "95.0, 10.0"\n---\n', "Bad.md") is None


def test_invalid_planning_values_are_dropped_not_coerced() -> None:
    text = """---
location: "40, -110"
visit_min: -5
best_months: [0, 13, "May"]
rating: 9
destination: "maybe"
---
"""
    p = parse_poi(text, "Odd.md")
    assert p is not None
    assert p.visit_min is None
    assert p.best_months is None
    assert p.rating is None
    assert p.destination is None


def test_empty_rating_key_is_none() -> None:
    p = parse_poi('---\nlocation: "40, -110"\nrating:\n---\n', "R.md")
    assert p is not None
    assert p.rating is None


def test_empty_best_months_means_no_season_info() -> None:
    p = parse_poi('---\nlocation: "40, -110"\nbest_months: []\n---\n', "S.md")
    assert p is not None
    assert p.best_months is None


def test_load_pois_reads_a_folder_and_reports_skips(tmp_path: Path) -> None:
    (tmp_path / "Example Canyon - Rimtown.md").write_text(FULL)
    (tmp_path / "Bare Place.md").write_text(BARE)
    (tmp_path / "Broken.md").write_text("# no frontmatter\n")
    (tmp_path / "notes.txt").write_text("ignored")
    (tmp_path / "assets").mkdir()
    pois, warnings = load_pois(tmp_path)
    assert [p.name for p in pois] == ["Bare Place", "Example Canyon - Rimtown"]
    assert warnings == ["POI note skipped (no frontmatter location): Broken.md"]


def test_load_pois_missing_folder_is_a_warning_not_a_crash(tmp_path: Path) -> None:
    pois, warnings = load_pois(tmp_path / "absent")
    assert pois == []
    assert len(warnings) == 1
    assert "not found" in warnings[0]
