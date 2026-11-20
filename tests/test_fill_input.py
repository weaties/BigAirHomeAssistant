"""Parsing and validating fill-up input from HA (#12). Rules: /fill-up-events."""

from __future__ import annotations

import json

import pytest

from bigair.fill_input import FillInput, parse_response, parse_text
from bigair.signals import FillType


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("22.4 4.19", (22.4, 4.19)),
        ("22.4", (22.4, None)),
        ("  22.4   $4.19 ", (22.4, 4.19)),
        ("22.4gal 4.19", (22.4, 4.19)),
        ("22,4 4,19", (22.4, 4.19)),  # comma decimal from some keyboards
    ],
)
def test_parse_text(text: str, expected: tuple[float, float | None]) -> None:
    assert parse_text(text) == expected


@pytest.mark.parametrize("text", ["", "lots", "4.19 22.4 1", "abc 4.19"])
def test_parse_text_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_text(text)


def _resp(**kw: object) -> str:
    base: dict[str, object] = {
        "fill_id": "f1",
        "kind": "full",
        "gallons": 22.4,
        "price_per_gal": 4.19,
    }
    base.update(kw)
    return json.dumps(base)


def test_form_response() -> None:
    assert parse_response(_resp()) == FillInput("f1", FillType.FULL, 22.4, 4.19)


def test_text_response_from_the_notification() -> None:
    raw = json.dumps({"fill_id": "f2", "kind": "partial", "text": "10.5 4.09"})
    assert parse_response(raw) == FillInput("f2", FillType.PARTIAL, 10.5, 4.09)


def test_zero_price_from_the_form_means_not_entered() -> None:
    assert parse_response(_resp(price_per_gal=0)).price_per_gal is None


def test_missing_fill_id_gets_one() -> None:
    a = parse_response(_resp(fill_id=""))
    assert a.fill_id.startswith("manual-")


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"gallons": 0.2}, "gallons"),
        ({"gallons": 61}, "gallons"),
        ({"gallons": 0}, "gallons"),
        ({"price_per_gal": 0.3}, "price"),
        ({"price_per_gal": 16}, "price"),
        ({"kind": "half"}, "full or partial"),
    ],
)
def test_invalid_values_are_rejected_with_a_reason(kw: dict[str, object], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_response(_resp(**kw))


def test_garbage_json_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_response("{nope")
