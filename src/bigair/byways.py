"""National Scenic Byways along a route, and choosing the most scenic route (#100).

The byway lines come from a local GeoJSON (USDOT ``US_Scenic_Byways``, layer
``National_Scenic_Byways``, western states, ``data/bigair/byways.geojson``),
indexed on a ``CELL_DEG`` grid. A route stretch counts as byway miles when its
midpoint is within ``TOL_MI`` of a byway line.

Slow mode asks Google for alternative routes and keeps the one with the **most
byway miles among those that fit the day** (window and mileage ceiling); ties go
to the shorter. A byway never justifies breaking the rules. If nothing fits,
Google's first route stands, and the day card says why.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bigair.fuel_sources import RouteInfo

CELL_DEG = 0.1
TOL_MI = 0.5
_EARTH_MI = 3958.76

Seg = tuple[str, tuple[float, float], tuple[float, float]]  # name, (lat, lon), (lat, lon)


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / CELL_DEG), math.floor(lon / CELL_DEG)


def _dist_mi(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance from p to segment ab, in a local flat projection around p."""
    kx = math.cos(math.radians(p[0]))
    ax, ay = (a[1] - p[1]) * kx, a[0] - p[0]
    bx, by = (b[1] - p[1]) * kx, b[0] - p[0]
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    return math.hypot(ax + t * dx, ay + t * dy) * math.pi / 180 * _EARTH_MI


@dataclass
class ByWayIndex:
    grid: dict[tuple[int, int], list[Seg]] = field(default_factory=lambda: defaultdict(list))
    names: set[str] = field(default_factory=set)

    @classmethod
    def empty(cls) -> ByWayIndex:
        return cls()

    def add(self, name: str, line: list[tuple[float, float]]) -> None:
        self.names.add(name)
        for a, b in zip(line, line[1:], strict=False):
            cells = {_cell(*a), _cell(*b)}
            # Long segments: also index the cells they pass through (sampled).
            n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / CELL_DEG)
            for k in range(1, n + 1):
                f = k / (n + 1)
                cells.add(_cell(a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
            for c in cells:
                self.grid[c].append((name, a, b))

    def near(self, p: tuple[float, float]) -> str | None:
        """The byway within TOL_MI of p, or None."""
        ci, cj = _cell(*p)
        best: tuple[float, str] | None = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for name, a, b in self.grid.get((ci + di, cj + dj), ()):
                    d = _dist_mi(p, a, b)
                    if d <= TOL_MI and (best is None or d < best[0]):
                        best = (d, name)
        return best[1] if best else None

    def byway_miles(self, route: RouteInfo) -> tuple[float, dict[str, float]]:
        per: dict[str, float] = {}
        pts, cum = route.points, route.cum_mi
        for i in range(len(pts) - 1):
            mid = ((pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2)
            name = self.near(mid)
            if name is not None:
                per[name] = per.get(name, 0.0) + (cum[i + 1] - cum[i])
        return sum(per.values()), per


def load_byways(path: Path) -> tuple[ByWayIndex | None, list[str]]:
    if not path.is_file():
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, [f"byways file unreadable ({e}): {path.name}"]
    idx = ByWayIndex()
    warnings: list[str] = []
    for n, feat in enumerate(data.get("features") or [], start=1):
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        if geom.get("type") == "LineString":
            lines = [geom["coordinates"]]
        elif geom.get("type") == "MultiLineString":
            lines = geom["coordinates"]
        else:
            warnings.append(f"byway feature {n} skipped (no line)")
            continue
        name = str(props.get("Trail_Name") or props.get("NAME") or f"Byway {n}").strip()
        for line in lines:
            idx.add(name, [(float(lat), float(lon)) for lon, lat, *_ in line])
    return idx, warnings


def choose_route(
    routes: list[RouteInfo], idx: ByWayIndex, window_min: float, max_mi: float
) -> tuple[RouteInfo, str]:
    """The most scenic route that fits the day, and a one-line reason."""
    n = len(routes)
    fits = [r for r in routes if r.duration_s / 60 <= window_min and r.total_mi <= max_mi]
    if not fits:
        return routes[0], f"none of {n} alternative{'s' * (n != 1)} fit the day; Google's first"
    scored = [(idx.byway_miles(r), r) for r in fits]
    (miles, per), best = min(scored, key=lambda x: (-x[0][0], x[1].total_mi))
    if miles <= 0:
        return best, f"no byways on any of {n}; took the shortest that fits"
    names = ", ".join(f"{k} ({v:.0f} mi)" for k, v in sorted(per.items(), key=lambda x: -x[1]))
    return best, f"most scenic of {n}: {miles:.0f} mi of byways ({names})"
