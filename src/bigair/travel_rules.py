"""Dan's travel rules for slow mode (#90).

Defaults are his rules (2026-11-25): at most 300 mi a day (a ceiling, never a
target: short days are good), leave at 10:00, arrive by 15:00, no driving after
dark, stay 3 nights, avoid interstates, and aim for the 70–80°F band. An optional
YAML file (``TRAVEL_RULES``) overrides any of them and sets tag weights for scoring::

    max_day_mi: 250
    depart: "09:30"
    arrive_by: "15:00"
    tag_weights: {hot-springs: 0.5, geology: 0.25}

The rules live in bigair, not in the vault, so the planner (and later Claude, #89)
never needs Dan's personal planning notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

MAX_SANE_DAY_MI = 600.0
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class TravelRules:
    max_day_mi: float = 300.0
    depart_h: int = 10
    depart_m: int = 0
    arrive_by_h: int = 15
    arrive_by_m: int = 0
    stay_nights: int = 3
    avoid_interstates: bool = True
    no_driving_after_dark: bool = True
    temp_band_f: tuple[float, float] = (70.0, 80.0)
    tag_weights: dict[str, float] = field(default_factory=dict)


def _hhmm(v: object, key: str) -> tuple[int, int]:
    m = _HHMM.match(str(v))
    if not m:
        raise ValueError(f"{key} must be HH:MM")
    return int(m.group(1)), int(m.group(2))


def load_rules(path: Path | None) -> TravelRules:
    """The rules file, or the defaults if there is none. Raises ValueError if it's bad."""
    if path is None or not path.is_file():
        return TravelRules()
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"travel rules {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("travel rules must be a mapping")
    known = {
        "max_day_mi", "depart", "arrive_by", "stay_nights", "avoid_interstates",
        "no_driving_after_dark", "temp_band_f", "tag_weights",
    }  # fmt: skip
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown travel rules: {', '.join(sorted(map(str, unknown)))}")
    r = TravelRules()
    kw: dict[str, Any] = {}
    if "max_day_mi" in data:
        mi = float(data["max_day_mi"])
        if not 0 < mi <= MAX_SANE_DAY_MI:
            raise ValueError(f"max_day_mi must be between 0 and {MAX_SANE_DAY_MI:g}")
        kw["max_day_mi"] = mi
    if "depart" in data:
        kw["depart_h"], kw["depart_m"] = _hhmm(data["depart"], "depart")
    if "arrive_by" in data:
        kw["arrive_by_h"], kw["arrive_by_m"] = _hhmm(data["arrive_by"], "arrive_by")
    if "stay_nights" in data:
        kw["stay_nights"] = int(data["stay_nights"])
    for key in ("avoid_interstates", "no_driving_after_dark"):
        if key in data:
            if not isinstance(data[key], bool):
                raise ValueError(f"{key} must be true or false")
            kw[key] = data[key]
    if "temp_band_f" in data:
        band = data["temp_band_f"]
        if not (isinstance(band, list) and len(band) == 2 and float(band[0]) < float(band[1])):
            raise ValueError("temp_band_f must be [low, high]")
        kw["temp_band_f"] = (float(band[0]), float(band[1]))
    if "tag_weights" in data:
        tw = data["tag_weights"]
        if not isinstance(tw, dict):
            raise ValueError("tag_weights must be a mapping of tag to weight")
        kw["tag_weights"] = {str(k): float(v) for k, v in tw.items()}
    return TravelRules(**{**r.__dict__, **kw})
