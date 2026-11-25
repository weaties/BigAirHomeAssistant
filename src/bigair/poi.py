"""Points of interest from Dan's road-trip vault (#88).

Each POI is one Markdown note with YAML frontmatter, in the format the vault's
Obsidian Map View plugin uses::

    ---
    location: "38.5000, -111.2000"
    tags: [trip-2027, geology]
    visit_min: 90                  # minutes for a detour visit (optional)
    visit_what: "Rim walk"         # what that time covers (optional)
    best_months: [4, 5, 9, 10]     # optional
    destination: true              # somewhere to stay 3 nights (optional)
    rating: 4                      # Dan's own 1-5 (optional)
    ---
    # Name
    > One-line summary.

The folder reaches the Pi through Syncthing, receive-only. bigair only reads it.
The notes are personal, so they never go in git, and tests use synthetic notes.

A wrong number is worse than none: a planning field that's missing or invalid
stays ``None``, so the planner shows "unknown" and never fills in a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

MAX_VISIT_MIN = 24 * 60

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", re.DOTALL)
_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class Poi:
    name: str
    lat: float
    lon: float
    tags: tuple[str, ...]
    summary: str | None
    visit_min: int | None = None
    visit_what: str | None = None
    best_months: tuple[int, ...] | None = None
    destination: bool | None = None
    rating: int | None = None


def _int_in(v: object, lo: int, hi: int) -> int | None:
    # bool is an int subclass; "visit_min: true" is not a number of minutes.
    if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
        return v
    return None


def _months(v: object) -> tuple[int, ...] | None:
    if not isinstance(v, list) or not v:
        return None
    months = [_int_in(m, 1, 12) for m in v]
    if any(m is None for m in months):
        return None
    return tuple(m for m in months if m is not None)


def _text(v: object) -> str | None:
    return v.strip() or None if isinstance(v, str) else None


def parse_poi(text: str, filename: str) -> Poi | None:
    """One note → a Poi, or None if it has no usable frontmatter location."""
    m = _FRONT.match(text)
    if not m:
        return None
    try:
        front: Any = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(front, dict):
        return None
    loc = _LATLON.match(str(front.get("location") or ""))
    if not loc:
        return None
    lat, lon = float(loc.group(1)), float(loc.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    name, summary = None, None
    for line in m.group(2).splitlines():
        s = line.strip()
        if name is None and s.startswith("# "):
            name = s[2:].strip()
        elif summary is None and s.startswith(">"):
            summary = s.lstrip(">").strip() or None
    if not name:
        name = filename.removesuffix(".md")

    tags = front.get("tags")
    dest = front.get("destination")
    return Poi(
        name=name,
        lat=lat,
        lon=lon,
        tags=tuple(str(t) for t in tags) if isinstance(tags, list) else (),
        summary=summary,
        visit_min=_int_in(front.get("visit_min"), 1, MAX_VISIT_MIN),
        visit_what=_text(front.get("visit_what")),
        best_months=_months(front.get("best_months")),
        destination=dest if isinstance(dest, bool) else None,
        rating=_int_in(front.get("rating"), 1, 5),
    )


def load_pois(folder: Path) -> tuple[list[Poi], list[str]]:
    """Every POI note in ``folder`` (not recursive), sorted by name, plus warnings."""
    if not folder.is_dir():
        return [], [f"POI folder {folder} not found; is Syncthing running?"]
    pois: list[Poi] = []
    warnings: list[str] = []
    for path in sorted(folder.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            warnings.append(f"POI note unreadable ({e}): {path.name}")
            continue
        p = parse_poi(text, path.name)
        if p is None:
            warnings.append(f"POI note skipped (no frontmatter location): {path.name}")
        else:
            pois.append(p)
    pois.sort(key=lambda p: p.name)
    return pois, warnings
