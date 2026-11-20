"""Fill-up prompt state machine (#11): prompt, re-prompt at park/ignition-on, expire."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from bigair.fill_prompt import CANDIDATE_TOPIC, EXPIRED_TOPIC, FillPrompter
from bigair.signals import FillCandidate, ObdSample

T0 = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)


def _cand(fill_id: str = "c1", sec: float = 0) -> FillCandidate:
    return FillCandidate(
        fill_id=fill_id,
        ts=T0 + timedelta(seconds=sec),
        est_gallons=22.0,
        odometer_mi=None,
        lat=39.5,
        lon=-111.0,
    )


def _s(sec: float, mph: float | None = 30.0, ignition: bool = True) -> ObdSample:
    return ObdSample(
        ts=T0 + timedelta(seconds=sec),
        ignition=ignition,
        speed_mph=mph,
        fuel_rate_gph=1.0 if ignition else None,
    )


def _prompts(msgs: list[tuple[str, str]]) -> list[dict[str, object]]:
    return [json.loads(p) for t, p in msgs if t == CANDIDATE_TOPIC]


def _drive_then_park(p: FillPrompter, start: int) -> tuple[list[tuple[str, str]], int]:
    """Drive 2 min, then sit at 0 mph for 70 s (a park). Returns messages and end time."""
    out: list[tuple[str, str]] = []
    t = start
    for _ in range(120):
        out += p.on_sample(_s(t, mph=40.0))
        t += 1
    for _ in range(70):
        out += p.on_sample(_s(t, mph=0.0))
        t += 1
    return out, t


def test_a_candidate_is_prompted_at_once() -> None:
    p = FillPrompter()
    (msg,) = _prompts(p.add(_cand(), T0))
    assert msg["fill_id"] == "c1"
    assert msg["est_gallons"] == 22.0
    assert msg["attempt"] == 1
    assert p.pending_ids == {"c1"}


def test_no_reprompt_before_ten_minutes() -> None:
    p = FillPrompter()
    p.add(_cand(), T0)
    out, _ = _drive_then_park(p, 10)  # parks ~3 min after the prompt
    assert _prompts(out) == []


def test_unanswered_prompt_is_repeated_at_the_next_park() -> None:
    p = FillPrompter()
    p.add(_cand(), T0)
    out, _ = _drive_then_park(p, 900)  # 15 min later
    (msg,) = _prompts(out)
    assert msg["attempt"] == 2


def test_unanswered_prompt_is_repeated_at_the_next_ignition_on() -> None:
    p = FillPrompter()
    p.add(_cand(), T0)
    p.on_sample(_s(60))
    p.on_sample(_s(61, mph=None, ignition=False))
    out = p.on_sample(_s(3600))  # engine back on an hour later
    (msg,) = _prompts(out)
    assert msg["attempt"] == 2


def test_later_reprompts_at_the_next_park_even_inside_ten_minutes() -> None:
    p = FillPrompter()
    p.add(_cand(), T0)
    assert p.later("c1", T0 + timedelta(seconds=30))
    out, _ = _drive_then_park(p, 60)
    assert [m["attempt"] for m in _prompts(out)] == [2]


def test_three_prompts_then_skipped_with_a_persistent_notice() -> None:
    p = FillPrompter(max_prompts=3)
    p.add(_cand(), T0)
    t = 0
    attempts: list[object] = []
    expired: list[dict[str, object]] = []
    for _ in range(4):
        out, t = _drive_then_park(p, t + 700)
        attempts += [m["attempt"] for m in _prompts(out)]
        expired += [json.loads(m) for topic, m in out if topic == EXPIRED_TOPIC]
    assert attempts == [2, 3]
    (e,) = expired
    assert e["fill_id"] == "c1"
    assert p.pending_ids == set()
    assert [s.fill_id for s in p.drain_skipped()] == ["c1"]


def test_resolve_returns_the_candidate_and_stops_prompts() -> None:
    p = FillPrompter()
    p.add(_cand(), T0)
    c = p.resolve("c1")
    assert c is not None and c.est_gallons == 22.0
    out, _ = _drive_then_park(p, 900)
    assert _prompts(out) == []
    assert p.resolve("c1") is None


def test_skip_removes_the_pending_fill() -> None:
    p = FillPrompter()
    p.add(_cand(), T0)
    c = p.skip("c1")
    assert c is not None and p.pending_ids == set()


def test_unknown_ids_are_ignored() -> None:
    p = FillPrompter()
    assert p.resolve("nope") is None
    assert not p.later("nope", T0)
    assert p.skip("nope") is None
