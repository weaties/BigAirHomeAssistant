"""HTTP edge: the trip planner page and its API (#75).

- GET  /                 → the planner page (``planner_page.html``)
- GET  /api/plan/last    → the last plan (JSON), or 404. Readable offline.
- POST /api/plan         → run a plan from the page's form (JSON) and cache it
- POST /api/pois         → re-run the POI search on the cached plan's route with
                           a new reach (#88), without re-planning, and update the cache
- POST /api/destinations → where to spend the next nights, from a start (#91)
- POST /api/chain        → a rough chain of legs and stays to a far goal (#92)
- GET  /api/claude/status, POST /api/claude/explain, GET /api/claude/proposals,
  POST /api/claude/proposals/<id>/accept|dismiss, POST /api/claude/chat (#93)
  → Claude (#89). Questions run
  one at a time, are capped per day, and Claude's monthly spend cap applies.
- GET  /health           → 200

Plans call paid Google APIs, and the page is reachable from the van Wi-Fi and
the tailnet without a login, so they are limited to one at a time and
``daily_limit`` per UTC day. POI searches only look up detour times (at most 25
cheap Routes calls each, and cached), so they get ``POI_LIMIT_FACTOR`` times as many.
The Google project's spend cap is the backstop.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from bigair.claude_service import chat_messages_from_json
from bigair.state_file import StateFileError, read_state, write_state
from bigair.trip_service import TripError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MAX_BODY = 16_384
CLAUDE_LIMIT_FACTOR = 5


class ClaudeApi(Protocol):
    def status(self) -> dict[str, Any]: ...
    def explain(self) -> dict[str, Any]: ...
    def chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]: ...
    def last_briefing(self) -> dict[str, Any] | None: ...
    def proposals(self) -> list[dict[str, Any]]: ...
    def decide(self, pid: str, accept: bool) -> dict[str, Any]: ...


POI_LIMIT_FACTOR = 5


def page_html() -> bytes:
    return (files("bigair") / "planner_page.html").read_bytes()


class _Limiter:
    def __init__(self, daily_limit: int, now_fn: Callable[[], datetime]) -> None:
        self.daily_limit = daily_limit
        self.now_fn = now_fn
        self.day = ""
        self.count = 0
        self.busy = threading.Lock()

    def take(self) -> bool:
        day = self.now_fn().strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.count = day, 0
        if self.count >= self.daily_limit:
            return False
        self.count += 1
        return True


def make_server(
    host: str,
    port: int,
    run_plan: Callable[[dict[str, Any]], dict[str, Any]],
    last_path: Path,
    daily_limit: int = 20,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_pois: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    run_destinations: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    run_chain: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    claude: ClaudeApi | None = None,
) -> ThreadingHTTPServer:
    limiter = _Limiter(daily_limit, now_fn)
    poi_limiter = _Limiter(daily_limit * POI_LIMIT_FACTOR, now_fn)
    dest_limiter = _Limiter(daily_limit, now_fn)
    claude_limiter = _Limiter(daily_limit * CLAUDE_LIMIT_FACTOR, now_fn)
    claude_busy = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            # http.server passes %-style args; loguru formats with {}, so
            # interpolate here or the line logs the raw "%s" placeholders.
            logger.debug("planner http {} {}", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: object) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, page_html(), "text/html; charset=utf-8")
            elif claude is not None and path == "/api/claude/status":
                self._json(200, claude.status())
            elif claude is not None and path == "/api/claude/proposals":
                self._json(200, claude.proposals())
            elif claude is not None and path == "/api/claude/briefing":
                last = claude.last_briefing()
                if last is None:
                    self._json(404, {"error": "no briefing yet"})
                else:
                    self._json(200, last)
            elif path == "/health":
                self._send(200, b"ok", "text/plain")
            elif path == "/api/plan/last":
                try:
                    last = read_state(last_path)
                except StateFileError as e:
                    logger.warning("cached plan unreadable: {}", e)
                    last = None
                if last is None:
                    self._json(404, {"error": "no plan yet"})
                else:
                    self._json(200, last)
            else:
                self._json(404, {"error": "not found"})

        def _body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._json(400, {"error": "bad request body"})
                return None
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except ValueError:
                self._json(400, {"error": "body must be a JSON object"})
                return None
            return payload

        def _last(self) -> dict[str, Any] | None:
            try:
                last = read_state(last_path)
            except StateFileError as e:
                logger.warning("cached plan unreadable: {}", e)
                return None
            return last if isinstance(last, dict) else None

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/pois" and run_pois is not None:
                self._pois()
                return
            if path == "/api/destinations" and run_destinations is not None:
                self._search(run_destinations, "destination search")
                return
            if path == "/api/chain" and run_chain is not None:
                self._search(run_chain, "chain sketch")
                return
            if claude is not None and path.startswith("/api/claude/"):
                self._claude(path)
                return
            if path != "/api/plan":
                self._json(404, {"error": "not found"})
                return
            payload = self._body()
            if payload is None:
                return
            if not limiter.busy.acquire(blocking=False):
                self._json(409, {"error": "a plan is already running"})
                return
            try:
                if not limiter.take():
                    self._json(429, {"error": f"daily limit of {daily_limit} plans reached"})
                    return
                try:
                    result = run_plan(payload)
                except TripError as e:
                    self._json(400, {"error": str(e)})
                    return
                except Exception as e:  # an upstream API failed; say so, keep serving
                    logger.warning("plan failed: {}", e)
                    self._json(502, {"error": f"planning failed: {e}"})
                    return
                try:
                    write_state(last_path, result, durable=False)
                except OSError as e:
                    logger.warning("couldn't cache the plan: {}", e)
                self._json(200, result)
            finally:
                limiter.busy.release()

        def _claude(self, path: str) -> None:
            assert claude is not None
            parts = path.strip("/").split("/")  # api claude proposals <id> accept
            if len(parts) == 5 and parts[2] == "proposals" and parts[4] in ("accept", "dismiss"):
                try:
                    self._json(200, claude.decide(parts[3], parts[4] == "accept"))
                except KeyError:
                    self._json(404, {"error": "no such open proposal"})
                return
            if path not in ("/api/claude/explain", "/api/claude/chat"):
                self._json(404, {"error": "not found"})
                return
            messages: list[dict[str, Any]] = []
            if path == "/api/claude/chat":
                payload = self._body()
                if payload is None:
                    return
                try:
                    messages = chat_messages_from_json(payload.get("messages"))
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                    return
            if not claude_busy.acquire(blocking=False):
                self._json(409, {"error": "Claude is already answering"})
                return
            try:
                if not claude_limiter.take():
                    self._json(429, {"error": "daily limit of Claude questions reached"})
                    return
                self._json(200, claude.chat(messages) if messages else claude.explain())
            except Exception as e:  # an upstream API failed; say so, keep serving
                logger.warning("Claude failed: {}", e)
                self._json(502, {"error": f"Claude failed: {e}"})
            finally:
                claude_busy.release()

        def _search(self, run: Callable[[dict[str, Any]], dict[str, Any]], what: str) -> None:
            payload = self._body()
            if payload is None:
                return
            if not limiter.busy.acquire(blocking=False):
                self._json(409, {"error": "a plan is already running"})
                return
            try:
                if not dest_limiter.take():
                    self._json(429, {"error": f"daily limit for {what} reached"})
                    return
                try:
                    self._json(200, run(payload))
                except TripError as e:
                    self._json(400, {"error": str(e)})
                except Exception as e:  # an upstream API failed; say so, keep serving
                    logger.warning("{} failed: {}", what, e)
                    self._json(502, {"error": f"{what} failed: {e}"})
            finally:
                limiter.busy.release()

        def _pois(self) -> None:
            assert run_pois is not None
            payload = self._body()
            if payload is None:
                return
            # Shares the plan lock: both read and write the cached plan.
            if not limiter.busy.acquire(blocking=False):
                self._json(409, {"error": "a plan is already running"})
                return
            try:
                last = self._last()
                if last is None:
                    self._json(409, {"error": "no plan yet; plan a route first"})
                    return
                if not poi_limiter.take():
                    self._json(429, {"error": "daily limit of POI searches reached"})
                    return
                try:
                    block = run_pois(payload, last)
                except TripError as e:
                    self._json(400, {"error": str(e)})
                    return
                except Exception as e:  # an upstream API failed; say so, keep serving
                    logger.warning("POI search failed: {}", e)
                    self._json(502, {"error": f"POI search failed: {e}"})
                    return
                last["pois"] = block
                try:
                    write_state(last_path, last, durable=False)
                except OSError as e:
                    logger.warning("couldn't cache the POI search: {}", e)
                self._json(200, block)
            finally:
                limiter.busy.release()

    return ThreadingHTTPServer((host, port), Handler)
