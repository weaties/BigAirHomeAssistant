"""Claude SDK edge (#89): request shape and error mapping, with the client faked."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2 as httpx
import pytest

from bigair import claude_io
from bigair.claude_service import ClaudeOffline


class _Msg:
    def __init__(self, d: dict[str, Any]) -> None:
        self.d = d

    def to_dict(self) -> dict[str, Any]:
        return self.d


class _FakeMessages:
    def __init__(self, result: object) -> None:
        self.result = result
        self.kwargs: dict[str, object] = {}

    def create(self, **kwargs: object) -> _Msg:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        assert isinstance(self.result, dict)
        return _Msg(self.result)


class _FakeClient:
    def __init__(self, result: object) -> None:
        self.beta = type("B", (), {"messages": _FakeMessages(result)})()


def test_send_adds_fallbacks_caching_and_returns_a_dict() -> None:
    client = _FakeClient({"content": [], "stop_reason": "end_turn", "usage": {}})
    send = claude_io.make_send(client=client)  # type: ignore[arg-type]
    assert send is not None
    out = send({"model": "claude-opus-5-5", "max_tokens": 10, "messages": []})
    assert out["stop_reason"] == "end_turn"
    kw = client.beta.messages.kwargs
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["model"] == "claude-opus-5-5"


def test_connection_errors_mean_offline() -> None:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = _FakeClient(anthropic.APIConnectionError(request=req))
    send = claude_io.make_send(client=client)  # type: ignore[arg-type]
    assert send is not None
    with pytest.raises(ClaudeOffline):
        send({"model": "m", "max_tokens": 1, "messages": []})


def test_without_a_key_there_is_no_sender() -> None:
    assert claude_io.make_send(api_key="") is None
