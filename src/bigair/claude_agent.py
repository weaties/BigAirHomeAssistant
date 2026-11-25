"""Claude's agent loop for BigAir (#89): Claude proposes, Python decides.

The loop is a plain tool-use loop over an injected ``send(params) -> response``
(``claude_io`` wires in the Anthropic SDK). It's written by hand rather than with
the SDK's beta tool runner so that the **spend cap can stop the loop between
calls**, and so the tests run without a network.

Rules this module enforces:

- **Tools are local functions.** Every one is read-only except proposals, which
  only take effect when Dan accepts them on the page. Claude never changes a
  plan, and never touches fuel numbers.
- **A failing tool goes back to Claude as an error result**, never a crash.
- **Spend:** each response's usage is costed at Opus 5.5 prices and added to a
  monthly ledger. A new call isn't started once less than ``MIN_START_USD`` of
  the cap is left. The Console spend limit is the real backstop; this one keeps
  bigair from getting there.
- **Turn limit:** at most ``max_turns`` calls per question.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

MODEL = "claude-opus-5-5"
EFFORT = "medium"
"""Explanations and planning chat; Opus 5.5's default, set explicitly."""
MAX_TOKENS = 16_000
MAX_TURNS = 8
MIN_START_USD = 0.50
"""Don't start a question with less than this left under the cap."""

# Opus 5.5, $ per million tokens (claude-api skill price table, 2026-11). Cache
# writes are the 5-minute kind, billed at 1.25 × input.
PRICE_IN = 4.00
PRICE_OUT = 20.00
PRICE_CACHE_READ = 0.20
PRICE_CACHE_WRITE = PRICE_IN * 1.25

Send = Callable[[dict[str, Any]], dict[str, Any]]


def cost_usd(usage: dict[str, Any]) -> float:
    def n(k: str) -> int:
        return int(usage.get(k) or 0)

    return (
        n("input_tokens") * PRICE_IN
        + n("output_tokens") * PRICE_OUT
        + n("cache_read_input_tokens") * PRICE_CACHE_READ
        + n("cache_creation_input_tokens") * PRICE_CACHE_WRITE
    ) / 1_000_000


@dataclass
class SpendLedger:
    """This month's Claude spend. Persisted by the caller (``to_dict``)."""

    cap_usd: float
    month: str
    spent_usd: float = 0.0

    def roll(self, now: datetime) -> None:
        m = now.strftime("%Y-%m")
        if m != self.month:
            self.month, self.spent_usd = m, 0.0

    def can_start(self) -> bool:
        return self.cap_usd - self.spent_usd >= MIN_START_USD

    def add(self, usd: float) -> None:
        self.spent_usd += usd

    def to_dict(self) -> dict[str, Any]:
        return {"month": self.month, "spent_usd": round(self.spent_usd, 6)}

    @classmethod
    def from_dict(
        cls, d: dict[str, Any] | None, cap_usd: float, now: datetime | None = None
    ) -> SpendLedger:
        d = d or {}
        month = str(d.get("month") or (now.strftime("%Y-%m") if now else ""))
        led = cls(cap_usd, month, float(d.get("spent_usd") or 0.0))
        if now is not None:
            led.roll(now)
        return led


@dataclass(frozen=True)
class Proposal:
    id: str
    kind: str
    """``route``: params hold ``destination``, ``via`` and ``mode``."""
    title: str
    params: dict[str, Any]
    reason: str
    created: datetime


@dataclass
class AgentContext:
    """What tools can reach during one question."""

    now: datetime
    proposals: list[Proposal] = field(default_factory=list)

    def propose(self, kind: str, title: str, params: dict[str, Any], reason: str) -> str:
        p = Proposal(uuid.uuid4().hex[:12], kind, title, params, reason, self.now)
        self.proposals.append(p)
        return p.id


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[AgentContext, dict[str, Any]], Any]

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


@dataclass(frozen=True)
class AgentResult:
    text: str
    stop: str
    """``done``, ``budget``, ``turns``, ``refusal`` or ``max_tokens``."""
    proposals: list[Proposal]
    tool_calls: list[str]
    cost_usd: float


def _run_tool(tools: dict[str, Tool], ctx: AgentContext, block: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "tool_result", "tool_use_id": block["id"]}
    tool = tools.get(block.get("name", ""))
    if tool is None:
        return {**out, "content": f"Unknown tool {block.get('name')!r}.", "is_error": True}
    try:
        result = tool.handler(ctx, dict(block.get("input") or {}))
    except Exception as e:  # report it to Claude; a tool failure isn't a crash
        return {**out, "content": f"{type(e).__name__}: {e}", "is_error": True}
    return {**out, "content": json.dumps(result, default=str)}


def _texts(content: list[dict[str, Any]]) -> str:
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


def run_agent(
    send: Send,
    system: str,
    tools: list[Tool],
    messages: list[dict[str, Any]],
    ledger: SpendLedger,
    ctx: AgentContext,
    max_turns: int = MAX_TURNS,
) -> AgentResult:
    """Answer one question, calling tools as Claude asks, within the budget."""
    by_name = {t.name: t for t in tools}
    defs = [t.definition() for t in tools]
    history = list(messages)
    called: list[str] = []
    spent = 0.0
    last_text = ""

    for _ in range(max_turns):
        if not ledger.can_start():
            msg = (
                f"Claude's monthly budget (${ledger.cap_usd:.0f}) is used up "
                f"(${ledger.spent_usd:.2f} spent this month)."
            )
            return AgentResult(msg, "budget", ctx.proposals, called, spent)
        resp = send(
            {
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "tools": defs,
                "messages": history,
                "output_config": {"effort": EFFORT},
            }
        )
        c = cost_usd(resp.get("usage") or {})
        ledger.add(c)
        spent += c
        content = list(resp.get("content") or [])
        stop = resp.get("stop_reason")
        last_text = _texts(content) or last_text

        if stop == "refusal":
            return AgentResult(
                "Claude declined to answer that.", "refusal", ctx.proposals, called, spent
            )
        if stop == "pause_turn":
            history.append({"role": "assistant", "content": content})
            continue
        uses = [b for b in content if b.get("type") == "tool_use"]
        if stop != "tool_use" or not uses:
            final = "max_tokens" if stop == "max_tokens" else "done"
            return AgentResult(last_text, final, ctx.proposals, called, spent)

        history.append({"role": "assistant", "content": content})
        results = []
        for b in uses:
            called.append(str(b.get("name")))
            results.append(_run_tool(by_name, ctx, b))
        history.append({"role": "user", "content": results})

    note = f"(Stopped after {max_turns} steps.)"
    return AgentResult(f"{last_text}\n\n{note}".strip(), "turns", ctx.proposals, called, spent)
