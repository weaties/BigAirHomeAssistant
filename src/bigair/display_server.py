"""HTTP edges serving dash frames to the eink-screen firmware and a browser preview.

Device server: the same contract as HelmLog's display endpoints, so one device
works with both (CLAUDE.md Decision #29):

- GET /api/display.raw?screen=<id>  (Bearer key) → 259200-byte 4-bit frame,
  X-Refresh-Seconds / X-Full-Refresh-Seconds headers. Unknown ids get the fuel
  screen; ?invert=1 flips polarity.
- GET /api/display.png?screen=<id>  (Bearer key) → preview.
- GET /api/display/menu             (Bearer key) → touch-menu tap regions (JSON).
- GET /api/display/menu.raw         (Bearer key) → touch-menu frame.
- GET /health                       → 200, no key.

Preview server (#62): no key, read-only PNGs plus a page to flip screens. It
must only ever be published on loopback; `tailscale serve` exposes it to the
tailnet (Decision #37). It never serves .raw frames.
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
    DEFAULT_SCREEN,
    SCREENS,
    DebugInfo,
    GaugeState,
    debug_from_dict,
    gauge_from_dict,
    menu_regions,
    pack_epd_4bit,
    render_menu,
    render_screen,
    with_van_voltage,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from PIL import Image


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


def _now() -> datetime:
    return datetime.now(UTC)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
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


def _frame_fn(
    state_fn: Callable[[], GaugeState],
    debug_fn: Callable[[], DebugInfo | None],
    now_fn: Callable[[], datetime],
    tz: str,
) -> Callable[[str], Image.Image]:
    def frame(screen: str) -> Image.Image:
        state = state_fn()
        debug = with_van_voltage(debug_fn(), state)
        return render_screen(screen, state, now_fn(), tz=tz, debug=debug)

    return frame


def _query(url_query: str) -> tuple[str, bool]:
    q = parse_qs(url_query)
    screen = q.get("screen", [DEFAULT_SCREEN])[0]
    invert = q.get("invert", ["0"])[0] in ("1", "true")
    return screen, invert


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
    now_fn: Callable[[], datetime] = _now,
) -> ThreadingHTTPServer:
    """The keyed server the LilyGo talks to."""
    if not api_key:
        raise ValueError("DISPLAY_API_KEY must be set")
    expected = f"Bearer {api_key}".encode()
    frame = _frame_fn(state_fn, debug_fn, now_fn, tz)
    routes = ("/api/display.raw", "/api/display.png", "/api/display/menu", "/api/display/menu.raw")

    class Handler(_Handler):
        def _authorized(self) -> bool:
            got = (self.headers.get("Authorization") or "").encode()
            return hmac.compare_digest(got, expected)

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            url = urlsplit(self.path)
            if url.path == "/health":
                self._send(200, b"ok\n", "text/plain")
                return
            if url.path not in routes:
                self._send(404, b"not found\n", "text/plain")
                return
            if not self._authorized():
                self._send(401, b"unauthorized\n", "text/plain")
                return
            screen, invert = _query(url.query)
            if url.path == "/api/display/menu":
                self._send(200, json.dumps(menu_regions()).encode(), "application/json")
                return
            img = render_menu() if url.path == "/api/display/menu.raw" else frame(screen)
            if url.path == "/api/display.png":
                if invert:
                    img = img.point(lambda v: 255 - v)
                self._send(200, _png(img), "image/png")
                return
            headers = {
                "X-Refresh-Seconds": str(refresh_s),
                "X-Full-Refresh-Seconds": str(full_refresh_s),
            }
            self._send(200, pack_epd_4bit(img, invert=invert), "application/octet-stream", headers)

    return ThreadingHTTPServer((host, port), Handler)


def make_preview_server(
    *,
    host: str,
    port: int,
    state_fn: Callable[[], GaugeState],
    tz: str,
    debug_fn: Callable[[], DebugInfo | None] = lambda: None,
    now_fn: Callable[[], datetime] = _now,
) -> ThreadingHTTPServer:
    """Keyless, read-only browser preview. Publish on loopback only (Decision #37)."""
    frame = _frame_fn(state_fn, debug_fn, now_fn, tz)
    page = preview_page().encode()

    class Handler(_Handler):
        def do_GET(self) -> None:  # noqa: N802 — http.server API
            url = urlsplit(self.path)
            if url.path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif url.path == "/health":
                self._send(200, b"ok\n", "text/plain")
            elif url.path == "/api/display.png":
                self._send(200, _png(frame(_query(url.query)[0])), "image/png")
            elif url.path == "/api/display/menu.png":
                self._send(200, _png(render_menu()), "image/png")
            elif url.path == "/api/display/menu":
                self._send(200, json.dumps(menu_regions()).encode(), "application/json")
            else:
                self._send(404, b"not found\n", "text/plain")

    return ThreadingHTTPServer((host, port), Handler)


def preview_page() -> str:
    """One self-contained page: a button per screen, the menu, auto-refresh."""
    buttons = "".join(f'<button data-screen="{sid}">{name}</button>' for sid, name in SCREENS)
    return _PAGE.replace("%BUTTONS%", buttons)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BigAir Dash</title>
<style>
:root { --bg: #f4f4f2; --fg: #1d1d1b; --muted: #6b6b66; --line: #c9c9c4;
        --on: #1d1d1b; --on-fg: #fff; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --bg: #1b1b1a; --fg: #ececea; --muted: #9a9a95;
          --line: #3a3a38; --on: #ececea; --on-fg: #1b1b1a; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
       font: 15px/1.4 system-ui, -apple-system, sans-serif; }
main { max-width: 980px; margin: 0 auto; }
h1 { font-size: 18px; margin: 0 0 12px; }
nav { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
button { font: inherit; padding: 8px 14px; border-radius: 8px; border: 1px solid var(--line);
         background: transparent; color: var(--fg); cursor: pointer; }
button[aria-pressed="true"] { background: var(--on); color: var(--on-fg); border-color: var(--on); }
.frame { border: 1px solid var(--line); border-radius: 6px; overflow: hidden; background: #fff; }
.frame img { display: block; width: 100%; height: auto; }
p { color: var(--muted); margin: 8px 0 0; }
</style></head>
<body><main>
<h1>BigAir dash preview</h1>
<nav>%BUTTONS%<button data-screen="menu">Touch menu</button></nav>
<div class="frame"><img id="frame" alt="Dash frame" width="960" height="540"></div>
<p id="note">Refreshes every 20 s. In the touch menu, click a tile to pick that screen,
as a tap on the device would.</p>
</main>
<script>
const img = document.getElementById("frame");
let screen = location.hash.slice(1) || "fuel";
let regions = [];
function show() {
  document.querySelectorAll("button").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.screen === screen)));
  const t = Date.now();
  img.src = screen === "menu" ? `/api/display/menu.png?t=${t}`
                              : `/api/display.png?screen=${encodeURIComponent(screen)}&t=${t}`;
  history.replaceState(null, "", "#" + screen);
}
document.querySelectorAll("button").forEach(b =>
  b.addEventListener("click", () => { screen = b.dataset.screen; show(); }));
img.addEventListener("click", e => {
  if (screen !== "menu") { screen = "menu"; show(); return; }  // a tap opens the menu
  const r = img.getBoundingClientRect();
  const x = (e.clientX - r.left) * 960 / r.width, y = (e.clientY - r.top) * 540 / r.height;
  const hit = regions.find(g => x >= g.x && x < g.x + g.w && y >= g.y && y < g.y + g.h);
  screen = hit ? hit.id : (localStorage.getItem("bigair-last") || "fuel");
  show();
});
img.addEventListener("load", () => {
  if (screen !== "menu") { try { localStorage.setItem("bigair-last", screen); } catch (e) {} }
});
fetch("/api/display/menu").then(r => r.json()).then(j => { regions = j; }).catch(() => {});
setInterval(() => { if (screen !== "menu") show(); }, 20000);
show();
</script>
</body></html>
"""
