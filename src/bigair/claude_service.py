"""The planner's Claude features (#89): explain the plan, keep proposals and spend.

State lives in two small files next to the cached plan:

- ``claude-spend.json``: this month's spend, written durably after every call,
  so a power cut can't reset the budget;
- ``claude-proposals.json``: Claude's open proposals, and the decided ones for
  the record.

When Claude can't be reached (no internet), it says "offline" and the rest of
the planner carries on. Without an API key, Claude is simply disabled.
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from loguru import logger

from bigair.claude_agent import AgentContext, Proposal, Send, SpendLedger, run_agent
from bigair.claude_tools import ToolDeps, build_tools, system_prompt
from bigair.state_file import StateFileError, read_state, write_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from bigair.travel_rules import TravelRules

EXPLAIN = (
    "Look at the plan on the planner page (last_plan). In a few sentences, explain why this "
    "route and these stops are a good day for us, or what's wrong with it. Check the "
    "proposed stops' notes and the weather where it matters. If a clearly better choice "
    "exists among our POIs within the rules, propose it with propose_route; otherwise don't."
)


MAX_CHAT_MESSAGES = 24
MAX_CHAT_CHARS = 4000


class ClaudeOffline(RuntimeError):
    """The API couldn't be reached (no internet, timeout)."""


def chat_messages_from_json(raw: object) -> list[dict[str, Any]]:
    """The page's conversation → API messages. The phone keeps the conversation; the
    server keeps none. Text only, alternating user/assistant, starting and ending
    with the user, bounded in length. Raises ValueError otherwise."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("messages must be a non-empty list")
    if len(raw) > MAX_CHAT_MESSAGES:
        raise ValueError(f"at most {MAX_CHAT_MESSAGES} messages; start a new chat")
    out: list[dict[str, Any]] = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ValueError("each message must be an object")
        role, text = m.get("role"), m.get("text")
        want = "user" if i % 2 == 0 else "assistant"
        if role != want:
            raise ValueError(f"message {i + 1} must be from the {want}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"message {i + 1} is empty")
        if len(text) > MAX_CHAT_CHARS:
            raise ValueError(f"message {i + 1} is too long")
        out.append({"role": role, "content": text.strip()})
    if out[-1]["role"] != "user":
        raise ValueError("the last message must be from the user")
    return out


class ClaudeService:
    def __init__(
        self,
        send: Send | None,
        deps: ToolDeps,
        rules: TravelRules,
        state_dir: Path,
        cap_usd: float,
        now: Callable[[], datetime],
    ) -> None:
        self.send = send
        self.deps = deps
        self.rules = rules
        self.spend_path = state_dir / "claude-spend.json"
        self.proposals_path = state_dir / "claude-proposals.json"
        self.cap_usd = cap_usd
        self.now = now
        self.lock = threading.Lock()

    # --- state ----------------------------------------------------------------

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            return read_state(path)
        except StateFileError as e:
            logger.warning("Claude state unreadable ({}): {}", path.name, e)
            return None

    def _ledger(self) -> SpendLedger:
        return SpendLedger.from_dict(self._read(self.spend_path), self.cap_usd, self.now())

    def _all_proposals(self) -> list[dict[str, Any]]:
        return list((self._read(self.proposals_path) or {}).get("proposals", []))

    # --- API ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        led = self._ledger()
        return {
            "enabled": self.send is not None,
            "month": led.month,
            "spent_usd": round(led.spent_usd, 2),
            "cap_usd": self.cap_usd,
            "can_start": led.can_start(),
        }

    def explain(self) -> dict[str, Any]:
        if self.send is None:
            return self._disabled()
        if self.deps.last_plan() is None:
            return {"text": "Plan a route first.", "stop": "no_plan", "proposals": []}
        return self._ask([{"role": "user", "content": EXPLAIN}], "explain")

    def chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Answer the last user message of a conversation (#93)."""
        if self.send is None:
            return self._disabled()
        return self._ask(messages, "chat")

    @staticmethod
    def _disabled() -> dict[str, Any]:
        msg = "Claude isn't set up (no ANTHROPIC_API_KEY on the Pi)."
        return {"text": msg, "stop": "disabled", "proposals": []}

    def _ask(self, messages: list[dict[str, Any]], what: str) -> dict[str, Any]:
        assert self.send is not None
        with self.lock:
            led = self._ledger()
            ctx = AgentContext(self.now())
            try:
                r = run_agent(
                    self.send,
                    system_prompt(self.rules),
                    build_tools(self.deps),
                    messages,
                    led,
                    ctx,
                )
            except ClaudeOffline as e:
                logger.info("Claude offline: {}", e)
                msg = "Claude is offline (no internet). The planner works without it."
                return {"text": msg, "stop": "offline", "proposals": []}
            finally:
                write_state(self.spend_path, led.to_dict(), durable=True)
            props = [self._proposal_json(p) for p in r.proposals]
            if props:
                stored = self._all_proposals() + props
                write_state(self.proposals_path, {"proposals": stored}, durable=False)
            logger.info(
                "Claude {}: {} tools, ${:.3f}, stop {}", what, len(r.tool_calls), r.cost_usd, r.stop
            )
            return {
                "text": r.text,
                "stop": r.stop,
                "proposals": props,
                "cost_usd": round(r.cost_usd, 3),
            }

    @staticmethod
    def _proposal_json(p: Proposal) -> dict[str, Any]:
        d = asdict(p)
        d["created"] = p.created.isoformat()
        d["status"] = "open"
        return d

    def proposals(self) -> list[dict[str, Any]]:
        return [p for p in self._all_proposals() if p.get("status") == "open"]

    def decide(self, pid: str, accept: bool) -> dict[str, Any]:
        """Mark an open proposal accepted or dismissed. KeyError if there's no such open one."""
        with self.lock:
            stored = self._all_proposals()
            for p in stored:
                if p.get("id") == pid and p.get("status") == "open":
                    p["status"] = "accepted" if accept else "dismissed"
                    p["decided"] = self.now().isoformat()
                    write_state(self.proposals_path, {"proposals": stored}, durable=False)
                    return p
            raise KeyError(pid)
