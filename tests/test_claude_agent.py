"""Claude agent loop, spend ledger and proposals (#89). The API is faked: no network."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from bigair.claude_agent import (
    AgentContext,
    SpendLedger,
    Tool,
    cost_usd,
    run_agent,
)

NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)


def _usage(
    inp: int = 1000, out: int = 200, cache_read: int = 0, cache_write: int = 0
) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


def _text(t: str, usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": t}],
        "stop_reason": "end_turn",
        "usage": usage or _usage(),
    }


def _tool_call(name: str, args: dict[str, Any], tid: str = "t1") -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": tid, "name": name, "input": args},
        ],
        "stop_reason": "tool_use",
        "usage": _usage(),
    }


class FakeApi:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.sent: list[dict[str, Any]] = []

    def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(params)
        return self.replies.pop(0)


def _ledger(spent: float = 0.0, cap: float = 25.0) -> SpendLedger:
    return SpendLedger(cap_usd=cap, month="2026-11", spent_usd=spent)


ECHO = Tool(
    name="echo",
    description="Echo the word back.",
    input_schema={
        "type": "object",
        "properties": {"word": {"type": "string"}},
        "required": ["word"],
        "additionalProperties": False,
    },
    handler=lambda ctx, args: {"echo": args["word"]},
)


# --- cost -------------------------------------------------------------------------


def test_cost_uses_opus_5_5_prices() -> None:
    # $4 / MTok in, $20 / MTok out, cache reads $0.20 / MTok, cache writes 1.25 × input.
    u = _usage(inp=1_000_000, out=100_000, cache_read=2_000_000, cache_write=400_000)
    assert cost_usd(u) == pytest.approx(4.0 + 2.0 + 0.4 + 2.0)


# --- ledger -----------------------------------------------------------------------


def test_ledger_rolls_over_each_month() -> None:
    led = _ledger(spent=24.0)
    led.roll(datetime(2026, 12, 1, tzinfo=UTC))
    assert (led.month, led.spent_usd) == ("2026-12", 0.0)


def test_ledger_blocks_when_too_little_is_left() -> None:
    assert _ledger(spent=24.99).can_start() is False
    assert _ledger(spent=10.0).can_start() is True


def test_ledger_round_trips_through_a_dict() -> None:
    led = _ledger(spent=3.25)
    again = SpendLedger.from_dict(led.to_dict(), cap_usd=25.0)
    assert (again.month, again.spent_usd, again.cap_usd) == ("2026-11", 3.25, 25.0)


# --- the loop ---------------------------------------------------------------------


def test_plain_answer_costs_and_returns_text() -> None:
    api = FakeApi([_text("Go via Kebler Pass.")])
    led = _ledger()
    r = run_agent(
        api, "system", [ECHO], [{"role": "user", "content": "why?"}], led, AgentContext(NOW)
    )
    assert r.text == "Go via Kebler Pass."
    assert r.stop == "done"
    assert led.spent_usd == pytest.approx(cost_usd(_usage()))
    p = api.sent[0]
    assert p["model"] == "claude-opus-5-5"
    assert p["tools"][0]["name"] == "echo"
    assert p["tools"][0]["strict"] is True
    assert p["output_config"] == {"effort": "medium"}
    assert "thinking" not in p  # Opus 5.5: adaptive is the default; disabled is a 400


def test_tool_calls_run_and_results_go_back_in_one_user_turn() -> None:
    api = FakeApi([_tool_call("echo", {"word": "hi"}), _text("Done.")])
    r = run_agent(
        api, "s", [ECHO], [{"role": "user", "content": "x"}], _ledger(), AgentContext(NOW)
    )
    assert r.text == "Done."
    second = api.sent[1]["messages"]
    assert second[1]["role"] == "assistant"  # the tool_use turn, unchanged
    assert second[1]["content"][1]["type"] == "tool_use"
    result = second[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "t1"
    assert '"echo": "hi"' in result["content"]
    assert r.tool_calls == ["echo"]


def test_a_failing_tool_is_reported_to_claude_not_raised() -> None:
    boom = Tool("boom", "Fails.", ECHO.input_schema, lambda ctx, a: 1 / 0)
    api = FakeApi([_tool_call("boom", {"word": "x"}), _text("Sorry, no data.")])
    r = run_agent(
        api, "s", [boom], [{"role": "user", "content": "x"}], _ledger(), AgentContext(NOW)
    )
    result = api.sent[1]["messages"][2]["content"][0]
    assert result["is_error"] is True
    assert "ZeroDivisionError" in result["content"]
    assert r.text == "Sorry, no data."


def test_an_unknown_tool_is_an_error_result() -> None:
    api = FakeApi([_tool_call("nope", {}), _text("ok")])
    run_agent(api, "s", [ECHO], [{"role": "user", "content": "x"}], _ledger(), AgentContext(NOW))
    assert api.sent[1]["messages"][2]["content"][0]["is_error"] is True


def test_the_budget_stops_the_loop_between_calls() -> None:
    led = _ledger(spent=24.9, cap=25.0)
    api = FakeApi([])
    r = run_agent(api, "s", [ECHO], [{"role": "user", "content": "x"}], led, AgentContext(NOW))
    assert r.stop == "budget"
    assert api.sent == []
    assert "budget" in r.text.lower()


def test_turn_limit_stops_a_runaway_loop() -> None:
    api = FakeApi([_tool_call("echo", {"word": str(i)}, f"t{i}") for i in range(10)])
    r = run_agent(
        api,
        "s",
        [ECHO],
        [{"role": "user", "content": "x"}],
        _ledger(),
        AgentContext(NOW),
        max_turns=3,
    )
    assert r.stop == "turns"
    assert len(api.sent) == 3


def test_a_refusal_is_reported_plainly() -> None:
    api = FakeApi([{"content": [], "stop_reason": "refusal", "usage": _usage()}])
    r = run_agent(
        api, "s", [ECHO], [{"role": "user", "content": "x"}], _ledger(), AgentContext(NOW)
    )
    assert r.stop == "refusal"
    assert r.text == "Claude declined to answer that."


def test_proposals_made_by_tools_are_collected() -> None:
    def propose(ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        return {"proposal_id": ctx.propose("route", args["word"], {"destination": "X"}, "nice")}

    t = Tool("propose", "Propose.", ECHO.input_schema, propose)
    api = FakeApi([_tool_call("propose", {"word": "Go to X"}), _text("Proposed.")])
    r = run_agent(api, "s", [t], [{"role": "user", "content": "x"}], _ledger(), AgentContext(NOW))
    assert len(r.proposals) == 1
    p = r.proposals[0]
    assert (p.kind, p.title, p.params, p.reason) == (
        "route",
        "Go to X",
        {"destination": "X"},
        "nice",
    )
    assert p.id and p.created == NOW
