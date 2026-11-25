"""Edge: the Anthropic SDK (#89). Turns ``claude_agent``'s request dicts into API calls.

- Model and parameters come from ``claude_agent``; this adds prompt caching
  (the system prompt and tools are the same on every call) and the server-side
  refusal fallback (``fallbacks: "default"``), so a declined request is re-run on
  a fallback model inside the same call.
- No internet (connection error, timeout) raises ``ClaudeOffline``. The planner
  shows "offline" and carries on.
- The key comes from ``ANTHROPIC_API_KEY`` in ``deploy/.env`` and is never logged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anthropic

from bigair.claude_service import ClaudeOffline

if TYPE_CHECKING:
    from bigair.claude_agent import Send

FALLBACK_BETA = "server-side-fallback-2026-07-01"
TIMEOUT_S = 120.0


def make_send(api_key: str | None = None, client: anthropic.Anthropic | None = None) -> Send | None:
    """A ``send`` for ``run_agent``, or None when there's no key (Claude disabled)."""
    if client is None:
        if not api_key:
            return None
        client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_S, max_retries=2)
    c = client

    def send(params: dict[str, Any]) -> dict[str, Any]:
        try:
            msg = c.beta.messages.create(
                **params,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                cache_control={"type": "ephemeral"},
            )
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise ClaudeOffline(str(e)) from e
        out: dict[str, Any] = msg.to_dict()
        return out

    return send
