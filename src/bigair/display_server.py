"""HTTP edge serving the dash gauge frame to the eink-screen firmware.

Same contract as HelmLog's display endpoints, so one device can serve both
(CLAUDE.md Decision #29):

- GET /api/display.raw  (Bearer key) → 259200-byte 4-bit frame,
  X-Refresh-Seconds / X-Full-Refresh-Seconds headers. ?screen= is ignored
  (one screen); ?invert=1 flips polarity.
- GET /api/display.png  (Bearer key) → preview.
- GET /health           → 200, no key.
- Touch-menu endpoints are not offered (404); the firmware treats that as a
  failed tap and carries on.
"""

from __future__ import annotations

import hmac
import io
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from bigair.display_render import (
    DebugInfo,
    GaugeState,
    debug_from_dict,
    gauge_from_dict,
    pack_epd_4bit,
    render_gauge,
    with_van_voltage,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def gauge_file_reader(path: Path) -> Callable[[], GaugeState]:
    """Read the fuel service's gauge feed on each frame; no data if missing or unreadable."""

    def read() -> GaugeState:
        try:
            return gauge_from_dict(json.loads(path.read_text()))
        except (OSError, ValueError) as e:
            logger.debug("no gauge feed from {}: {}", path, e)
            return GaugeState(tank_gal=None, range_mi=None)

    return read


def debug_file_reader(path: Path) -> Callable[[], DebugInfo | None]:
    """Read the host's debug JSON on each frame; None if missing or unreadable."""

    def read() -> DebugInfo | None:
        try:
            return debug_from_dict(json.loads(path.read_text()))
        except (OSError, ValueError) as e:
            logger.debug("no debug info from {}: {}", path, e)
            return None

    return read


def make_server(
    *,
    host: str,
    port: int,
    api_key: str,
    state_fn: Callable[[], GaugeState],
    refresh_s: int,
    full_refresh_s: int,
    tz: str,
    debug_fn: Callable[[], DebugInfo | None] = lambda: None,
) -> ThreadingHTTPServer:
    if not api_key:
        raise ValueError("DISPLAY_API_KEY must be set")
    expected = f"Bearer {api_key}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "bigair-display"

        def log_message(self, fmt: str, *args: object) -> None:
            # http.server passes %-style args; loguru formats with {}, so
            # interpolate here or the line logs the raw "%s" placeholders.
            logger.debug("display http {} {}", self.address_string(), fmt % args)

        def _send(
            self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            got = (self.headers.get("Authorization") or "").encode()
            return hmac.compare_digest(got, expected)

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            url = urlsplit(self.path)
            if url.path == "/health":
                self._send(200, b"ok\n", "text/plain")
                return
            if url.path not in ("/api/display.raw", "/api/display.png"):
                self._send(404, b"not found\n", "text/plain")
                return
            if not self._authorized():
                self._send(401, b"unauthorized\n", "text/plain")
                return
            invert = parse_qs(url.query).get("invert", ["0"])[0] in ("1", "true")
            state = state_fn()
            debug = with_van_voltage(debug_fn(), state)
            img = render_gauge(state, datetime.now(UTC), tz=tz, debug=debug)
            if url.path == "/api/display.png":
                if invert:
                    img = img.point(lambda v: 255 - v)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                self._send(200, buf.getvalue(), "image/png")
                return
            self._send(
                200,
                pack_epd_4bit(img, invert=invert),
                "application/octet-stream",
                {
                    "X-Refresh-Seconds": str(refresh_s),
                    "X-Full-Refresh-Seconds": str(full_refresh_s),
                },
            )

    return ThreadingHTTPServer((host, port), Handler)
