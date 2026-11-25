"""Trip planner HTTP edge (#75): page, cached plan, limits, errors."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from bigair.planner_server import make_server
from bigair.trip_service import TripError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)


def _serve(
    tmp_path: Path, run: Callable[[dict[str, Any]], dict[str, Any]], limit: int = 20
) -> Iterator[str]:
    server = make_server("127.0.0.1", 0, run, tmp_path / "plan.json", limit, lambda: NOW)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _req(url: str, body: object | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            return r.status, json.loads(raw) if "json" in ctype else raw
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def ok_url(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path, lambda p: {"plan": p["destination"], "generated_at": "x"})


def test_page_is_served(ok_url: str) -> None:
    status, body = _req(f"{ok_url}/")
    assert status == 200
    assert b"<title>Trip Planner</title>" in body
    assert b"leaflet/1.9.4" in body


def test_plan_then_last(ok_url: str) -> None:
    assert _req(f"{ok_url}/api/plan/last")[0] == 404
    status, body = _req(f"{ok_url}/api/plan", {"origin": "A", "destination": "B"})
    assert (status, body["plan"]) == (200, "B")
    status, last = _req(f"{ok_url}/api/plan/last")
    assert (status, last["plan"]) == (200, "B")


def test_bad_body(ok_url: str) -> None:
    req = urllib.request.Request(f"{ok_url}/api/plan", data=b"[1,2]", method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 400


def test_trip_error_is_400_and_other_errors_502(tmp_path: Path) -> None:
    def run(p: dict[str, Any]) -> dict[str, Any]:
        if p.get("kind") == "trip":
            raise TripError("no GPS fix")
        raise RuntimeError("Google said no")

    gen = _serve(tmp_path, run)
    url = next(gen)
    assert _req(f"{url}/api/plan", {"kind": "trip"}) == (400, {"error": "no GPS fix"})
    status, body = _req(f"{url}/api/plan", {"kind": "other"})
    assert status == 502
    assert "Google said no" in body["error"]
    assert not (tmp_path / "plan.json").exists()  # failures aren't cached
    next(gen, None)


def test_daily_limit(tmp_path: Path) -> None:
    gen = _serve(tmp_path, lambda p: {"ok": True}, limit=2)
    url = next(gen)
    codes = [_req(f"{url}/api/plan", {"n": i})[0] for i in range(3)]
    assert codes == [200, 200, 429]
    next(gen, None)


def test_one_plan_at_a_time(tmp_path: Path) -> None:
    gate = threading.Event()
    started = threading.Event()

    def slow(p: dict[str, Any]) -> dict[str, Any]:
        started.set()
        gate.wait(5)
        return {"ok": True}

    gen = _serve(tmp_path, slow)
    url = next(gen)
    first: list[int] = []
    t = threading.Thread(target=lambda: first.append(_req(f"{url}/api/plan", {})[0]))
    t.start()
    started.wait(5)
    assert _req(f"{url}/api/plan", {})[0] == 409
    gate.set()
    t.join(5)
    assert first == [200]
    next(gen, None)


def test_access_log_names_the_request(ok_url: str) -> None:
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG", format="{message}")
    try:
        _req(f"{ok_url}/health")
    finally:
        logger.remove(sink)
    (line,) = [m for m in lines if "planner http" in m]
    assert "127.0.0.1" in line
    assert '"GET /health HTTP/1.1" 200' in line
    assert "%s" not in line


# --- POI reach selector (#88) ----------------------------------------------------


def _serve_pois(
    tmp_path: Path, run_pois: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
) -> Iterator[str]:
    server = make_server(
        "127.0.0.1",
        0,
        lambda p: {"plan": p["destination"], "pois": {"hits": []}},
        tmp_path / "plan.json",
        20,
        lambda: NOW,
        run_pois=run_pois,
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_pois_reuse_the_cached_plan_and_update_it(tmp_path: Path) -> None:
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def run_pois(payload: dict[str, Any], last: dict[str, Any]) -> dict[str, Any]:
        seen.append((payload, last))
        return {"hits": [{"name": "Near Springs"}], "reach": payload["reach"]}

    gen = _serve_pois(tmp_path, run_pois)
    url = next(gen)
    assert _req(f"{url}/api/pois", {"reach": {"mode": "detour", "hours": 1}})[0] == 409
    _req(f"{url}/api/plan", {"origin": "A", "destination": "B"})
    status, block = _req(f"{url}/api/pois", {"reach": {"mode": "detour", "hours": 1}})
    assert status == 200
    assert block["hits"] == [{"name": "Near Springs"}]
    assert seen[0][1]["plan"] == "B"
    _, last = _req(f"{url}/api/plan/last")
    assert last["pois"]["hits"] == [{"name": "Near Springs"}]  # cache updated
    next(gen, None)


def test_pois_trip_error_is_400(tmp_path: Path) -> None:
    def run_pois(payload: dict[str, Any], last: dict[str, Any]) -> dict[str, Any]:
        raise TripError("reach mode must be one of detour, each_way, ahead")

    gen = _serve_pois(tmp_path, run_pois)
    url = next(gen)
    _req(f"{url}/api/plan", {"origin": "A", "destination": "B"})
    status, body = _req(f"{url}/api/pois", {"reach": {"mode": "fly"}})
    assert status == 400
    assert "reach mode" in body["error"]
    next(gen, None)


def test_pois_unavailable_without_a_poi_runner(ok_url: str) -> None:
    _req(f"{ok_url}/api/plan", {"origin": "A", "destination": "B"})
    assert _req(f"{ok_url}/api/pois", {"reach": {}})[0] == 404


def test_destinations_endpoint_runs_and_limits(tmp_path: Path) -> None:
    def run_dest(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("origin") == "bad":
            raise TripError("give a starting place (or Here)")
        return {"options": [{"name": "Springs"}]}

    server = make_server(
        "127.0.0.1", 0, lambda p: {}, tmp_path / "plan.json", 2, lambda: NOW,
        run_destinations=run_dest,
    )  # fmt: skip
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _req(f"{url}/api/destinations", {"origin": "here"})
        assert (status, body["options"][0]["name"]) == (200, "Springs")
        assert _req(f"{url}/api/destinations", {"origin": "bad"})[0] == 400
        # 2 a day, and errors count
        assert _req(f"{url}/api/destinations", {"origin": "here"})[0] == 429
    finally:
        server.shutdown()
        server.server_close()


def test_chain_endpoint(tmp_path: Path) -> None:
    server = make_server(
        "127.0.0.1", 0, lambda p: {}, tmp_path / "plan.json", 2, lambda: NOW,
        run_chain=lambda p: {"legs": [{"name": "Open spot"}]},
    )  # fmt: skip
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _req(f"{url}/api/chain", {"origin": "here", "goal": "X", "by": "2027-06-01"})
        assert (status, body["legs"][0]["name"]) == (200, "Open spot")
    finally:
        server.shutdown()
        server.server_close()


# --- Claude (#89) ------------------------------------------------------------------


class _FakeClaude:
    def __init__(self) -> None:
        self.decided: list[tuple[str, bool]] = []

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "spent_usd": 1.5, "cap_usd": 25.0}

    def explain(self) -> dict[str, Any]:
        return {"text": "Because.", "stop": "done", "proposals": [{"id": "p1"}]}

    def proposals(self) -> list[dict[str, Any]]:
        return [{"id": "p1", "status": "open"}]

    def decide(self, pid: str, accept: bool) -> dict[str, Any]:
        if pid != "p1":
            raise KeyError(pid)
        self.decided.append((pid, accept))
        return {"id": pid, "status": "accepted" if accept else "dismissed"}


def test_claude_endpoints(tmp_path: Path) -> None:
    fake = _FakeClaude()
    server = make_server(
        "127.0.0.1", 0, lambda p: {}, tmp_path / "plan.json", 2, lambda: NOW, claude=fake
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _req(f"{url}/api/claude/status")[1]["spent_usd"] == 1.5
        status, body = _req(f"{url}/api/claude/explain", {})
        assert (status, body["text"]) == (200, "Because.")
        assert _req(f"{url}/api/claude/proposals")[1] == [{"id": "p1", "status": "open"}]
        status, body = _req(f"{url}/api/claude/proposals/p1/accept", {})
        assert (status, body["status"]) == (200, "accepted")
        assert _req(f"{url}/api/claude/proposals/nope/dismiss", {})[0] == 404
        assert fake.decided == [("p1", True)]
    finally:
        server.shutdown()
        server.server_close()


def test_claude_endpoints_absent_without_claude(ok_url: str) -> None:
    assert _req(f"{ok_url}/api/claude/status")[0] == 404


def test_claude_chat_endpoint(tmp_path: Path) -> None:
    class Chatty(_FakeClaude):
        def chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
            return {"text": f"you said {messages[-1]['content']}", "stop": "done", "proposals": []}

    server = make_server(
        "127.0.0.1", 0, lambda p: {}, tmp_path / "plan.json", 2, lambda: NOW, claude=Chatty()
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _req(
            f"{url}/api/claude/chat", {"messages": [{"role": "user", "text": "hi"}]}
        )
        assert (status, body["text"]) == (200, "you said hi")
        status, body = _req(
            f"{url}/api/claude/chat", {"messages": [{"role": "assistant", "text": "x"}]}
        )
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()


def test_claude_briefing_endpoint(tmp_path: Path) -> None:
    class Briefed(_FakeClaude):
        def last_briefing(self) -> dict[str, Any] | None:
            return {"date": "2026-11-25", "text": "Tomorrow: 180 mi."}

    server = make_server(
        "127.0.0.1", 0, lambda p: {}, tmp_path / "plan.json", 2, lambda: NOW, claude=Briefed()
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _req(f"{url}/api/claude/briefing")[1]["text"] == "Tomorrow: 180 mi."
    finally:
        server.shutdown()
        server.server_close()
