"""HTTP edge: the trip planner page and its API (#75).

- GET  /                 → the planner page (``planner_page.html``)
- GET  /api/plan/last    → the last plan (JSON), or 404. Readable offline.
- POST /api/plan         → run a plan from the page's form (JSON) and cache it
- GET  /health           → 200

Plans call paid Google APIs, and the page is reachable from the van Wi-Fi and
the tailnet without a login, so they are limited to one at a time and
``daily_limit`` per UTC day. The Google project's spend cap is the backstop.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from loguru import logger

from bigair.state_file import StateFileError, read_state, write_state
from bigair.trip_service import TripError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MAX_BODY = 16_384


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
) -> ThreadingHTTPServer:
    limiter = _Limiter(daily_limit, now_fn)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            logger.debug("planner http: " + fmt, *args)

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

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/api/plan":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._json(400, {"error": "bad request body"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except ValueError:
                self._json(400, {"error": "body must be a JSON object"})
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

    return ThreadingHTTPServer((host, port), Handler)
