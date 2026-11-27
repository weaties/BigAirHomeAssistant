"""Destination candidates from local files on the Pi (#91).

- **iOverlander:** GPX exports Dan downloads from iOverlander (there's no official
  API) into ``IOVERLANDER_DIR``. Only campsite categories are kept.
- **Public land:** a GeoJSON of BLM / USFS units (for example a regional PAD-US or
  BLM Surface Management Agency extract) at ``PUBLIC_LAND_FILE``. Each unit
  becomes one candidate at a point guaranteed to be inside its largest polygon.

Both files are optional. When one is missing it adds nothing, and a bad file is
a warning, never a crash. The GPX files are Dan's own downloads, read from local
disk.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET  # noqa: S405 — Dan's own local exports, not network input
from typing import TYPE_CHECKING, Any

from bigair.destinations import Destination

if TYPE_CHECKING:
    from pathlib import Path

CAMP_TYPES = {
    "wild camping",
    "informal campsite",
    "established campground",
    "campground",
    "dispersed camping",
}
NAME_KEYS = ("Unit_Nm", "UNIT_NAME", "NAME", "Name", "name")
MANAGER_KEYS = ("Mang_Name", "ADMIN_AGENCY_CODE", "AGENCY", "Agency", "manager")


def _tag(el: ET.Element, name: str) -> str | None:
    for child in el:
        if child.tag.rsplit("}", 1)[-1] == name and child.text:
            return child.text.strip() or None
    return None


def load_ioverlander(folder: Path) -> tuple[list[Destination], list[str]]:
    if not folder.is_dir():
        return [], []
    out: list[Destination] = []
    warnings: list[str] = []
    for path in sorted(folder.glob("*.gpx")):
        try:
            root = ET.parse(path).getroot()  # noqa: S314 — local file Dan exported
        except (ET.ParseError, OSError) as e:
            warnings.append(f"iOverlander file skipped ({e}): {path.name}")
            continue
        for wpt in root.iter():
            if wpt.tag.rsplit("}", 1)[-1] != "wpt":
                continue
            kind = _tag(wpt, "type")
            if kind is None or kind.lower() not in CAMP_TYPES:
                continue
            try:
                lat, lon = float(wpt.attrib["lat"]), float(wpt.attrib["lon"])
            except (KeyError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            name = _tag(wpt, "name") or f"{kind} {lat:.3f},{lon:.3f}"
            out.append(Destination(name, lat, lon, "ioverlander", kind, _tag(wpt, "desc")))
    return out, warnings


def point_in_polygon(lat: float, lon: float, ring: list[list[float]]) -> bool:
    """Ray casting on a GeoJSON ring of [lon, lat] pairs."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _area_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    """(signed area, centroid lat, centroid lon) in degrees, good enough for a unit."""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:], strict=False):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if a == 0:
        return 0.0, ring[0][1], ring[0][0]
    return a / 2, cy / (3 * a), cx / (3 * a)


def _inside_point(ring: list[list[float]]) -> tuple[float, float]:
    """The centroid if it's inside; otherwise the middle of the widest horizontal span
    through the centroid's latitude (always inside)."""
    _, lat, lon = _area_centroid(ring)
    if point_in_polygon(lat, lon, ring):
        return lat, lon
    xs: list[float] = []
    for (x0, y0), (x1, y1) in zip(ring, ring[1:], strict=False):
        if (y0 > lat) != (y1 > lat):
            xs.append(x0 + (lat - y0) * (x1 - x0) / (y1 - y0))
    xs.sort()
    spans = [(xs[i + 1] - xs[i], xs[i]) for i in range(0, len(xs) - 1, 2)]
    if not spans:
        return ring[0][1], ring[0][0]
    width, start = max(spans)
    return lat, start + width / 2


def _first(props: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def load_public_land(path: Path) -> tuple[list[Destination], list[str]]:
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], [f"public land file unreadable ({e}): {path.name}"]
    out: list[Destination] = []
    warnings: list[str] = []
    for n, feat in enumerate(data.get("features") or [], start=1):
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        if geom.get("type") == "Polygon":
            rings = [geom["coordinates"][0]]
        elif geom.get("type") == "MultiPolygon":
            rings = [poly[0] for poly in geom["coordinates"]]
        else:
            warnings.append(f"public land feature {n} skipped (no polygon)")
            continue
        ring = max(rings, key=lambda r: abs(_area_centroid(r)[0]))
        lat, lon = _inside_point(ring)
        kind = _first(props, MANAGER_KEYS)
        name = _first(props, NAME_KEYS) or f"{kind or 'Public land'} unit {n}"
        out.append(Destination(name, lat, lon, "public_land", kind, None))
    return out, warnings


WALMART_QUERY = "Walmart Supercenter"
"""Search-along-route query for slow mode's overnight fallback (#117)."""


def decode_walmarts(body: dict[str, Any]) -> list[Destination]:
    """One Places search page → the Walmarts in it, as overnight fallbacks (#117).

    Text search also returns nearby non-Walmart places, so only names with
    "Walmart" in them are kept. Not every store allows overnight parking.
    """
    out: list[Destination] = []
    for p in body.get("places") or []:
        name = str((p.get("displayName") or {}).get("text") or "")
        loc = p.get("location") or {}
        if "walmart" not in name.lower() or "latitude" not in loc or "longitude" not in loc:
            continue
        out.append(
            Destination(
                name,
                float(loc["latitude"]),
                float(loc["longitude"]),
                "walmart",
                None,
                p.get("formattedAddress"),
            )  # fmt: skip
        )
    return out
