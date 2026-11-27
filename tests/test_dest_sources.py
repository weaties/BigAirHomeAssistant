"""Destination sources from local files (#91): iOverlander GPX exports, public-land GeoJSON."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bigair.dest_sources import load_ioverlander, load_public_land, point_in_polygon

if TYPE_CHECKING:
    from pathlib import Path

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="synthetic" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="38.1000" lon="-111.5000"><name>Mesa Pullout</name><desc>Flat, quiet.</desc>
    <type>Wild Camping</type></wpt>
  <wpt lat="38.2000" lon="-111.6000"><name>Town Lot</name><type>Informal Campsite</type></wpt>
  <wpt lat="38.3000" lon="-111.7000"><name>Water Spigot</name><type>Water</type></wpt>
  <wpt lat="95" lon="0"><name>Broken</name><type>Wild Camping</type></wpt>
</gpx>
"""


def test_ioverlander_gpx_keeps_campsites_only(tmp_path: Path) -> None:
    (tmp_path / "utah.gpx").write_text(GPX)
    (tmp_path / "notes.txt").write_text("ignored")
    dests, warnings = load_ioverlander(tmp_path)
    assert [(d.name, d.source, d.kind) for d in dests] == [
        ("Mesa Pullout", "ioverlander", "Wild Camping"),
        ("Town Lot", "ioverlander", "Informal Campsite"),
    ]
    assert dests[0].note == "Flat, quiet."
    assert (dests[0].lat, dests[0].lon) == (38.1, -111.5)
    assert warnings == []


def test_ioverlander_bad_file_is_a_warning(tmp_path: Path) -> None:
    (tmp_path / "bad.gpx").write_text("<gpx><wpt")
    dests, warnings = load_ioverlander(tmp_path)
    assert dests == []
    assert warnings and "bad.gpx" in warnings[0]


def test_ioverlander_missing_folder_is_empty_and_quiet(tmp_path: Path) -> None:
    assert load_ioverlander(tmp_path / "absent") == ([], [])


SQUARE = [[-112.0, 38.0], [-111.0, 38.0], [-111.0, 39.0], [-112.0, 39.0], [-112.0, 38.0]]
# A "U" shape whose centroid falls in the notch, outside the polygon.
U = [[-110, 38], [-109, 38], [-109, 39], [-109.3, 39], [-109.3, 38.3], [-109.7, 38.3],
     [-109.7, 39], [-110, 39], [-110, 38]]  # fmt: skip


def _geojson(features: list[dict[str, object]]) -> str:
    return json.dumps({"type": "FeatureCollection", "features": features})


def test_public_land_units_become_destinations_inside_their_polygon(tmp_path: Path) -> None:
    f = tmp_path / "land.geojson"
    f.write_text(
        _geojson(
            [
                {
                    "type": "Feature",
                    "properties": {"Unit_Nm": "Square BLM", "Mang_Name": "BLM"},
                    "geometry": {"type": "Polygon", "coordinates": [SQUARE]},
                },
                {
                    "type": "Feature",
                    "properties": {"NAME": "U Forest", "AGENCY": "USFS"},
                    "geometry": {"type": "MultiPolygon", "coordinates": [[U]]},
                },
                {"type": "Feature", "properties": {}, "geometry": None},
            ]
        )
    )
    dests, warnings = load_public_land(f)
    assert [(d.name, d.source, d.kind) for d in dests] == [
        ("Square BLM", "public_land", "BLM"),
        ("U Forest", "public_land", "USFS"),
    ]
    sq, u = dests
    assert (round(sq.lat, 3), round(sq.lon, 3)) == (38.5, -111.5)
    assert point_in_polygon(u.lat, u.lon, U)  # never a point in the notch
    assert warnings == ["public land feature 3 skipped (no polygon)"]


def test_public_land_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_public_land(tmp_path / "absent.geojson") == ([], [])
