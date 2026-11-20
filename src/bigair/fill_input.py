"""Parse and validate fill-up input from Home Assistant (#12) — Critical tier.

HA only collects input (CLAUDE.md: no logic in templates). It publishes JSON
on `bigair/fill_up/response`:

- form:          {"fill_id", "kind": "full"|"partial", "gallons", "price_per_gal"}
- notification:  {"fill_id", "kind", "text": "22.4 4.19"}  (gallons, then price)

Validation ranges come from /fill-up-events. Pure: text in, FillInput or a
ValueError with a reason Dan can read on the phone.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from bigair.signals import FillType

GALLONS_RANGE = (0.5, 60.0)
PRICE_RANGE = (0.5, 15.0)


@dataclass(frozen=True)
class FillInput:
    fill_id: str
    kind: FillType
    gallons: float
    price_per_gal: float | None


def parse_text(text: str) -> tuple[float, float | None]:
    """'22.4 4.19' → (22.4, 4.19); '22.4' → (22.4, None)."""
    cleaned = re.sub(r"(?i)gal(lons?)?|\$", " ", text).replace(",", ".")
    tokens = cleaned.split()
    if not 1 <= len(tokens) <= 2:
        raise ValueError("type gallons, then price, e.g. 22.4 4.19")
    try:
        numbers = [float(t) for t in tokens]
    except ValueError:
        raise ValueError("type gallons, then price, e.g. 22.4 4.19") from None
    return numbers[0], numbers[1] if len(numbers) == 2 else None


def _check(value: float, lo: float, hi: float, what: str) -> float:
    if not lo <= value <= hi:
        raise ValueError(f"{what} {value:g} is outside {lo:g}–{hi:g}")
    return value


def parse_response(raw: str) -> FillInput:
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"unreadable fill-up message: {e}") from e
    if not isinstance(d, dict):
        raise ValueError("unreadable fill-up message")
    try:
        kind = FillType(str(d.get("kind", "")).strip().lower())
    except ValueError:
        raise ValueError("fill type must be full or partial") from None
    if "text" in d:
        gallons, price = parse_text(str(d["text"]))
    else:
        try:
            gallons = float(d.get("gallons") or 0)
            price_raw = float(d.get("price_per_gal") or 0)
        except (TypeError, ValueError):
            raise ValueError("gallons and price must be numbers") from None
        price = price_raw or None  # the form's 0 means "not entered"
    _check(gallons, *GALLONS_RANGE, "gallons")
    if price is not None:
        _check(price, *PRICE_RANGE, "price")
    fill_id = str(d.get("fill_id") or "").strip() or f"manual-{int(time.time())}"
    return FillInput(fill_id, kind, gallons, price)
