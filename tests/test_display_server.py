"""HTTP contract served to the eink-screen firmware (same as HelmLog's /api/display.raw)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from bigair.display_render import FRAME_BYTES, SCREENS, GaugeState
from bigair.display_server import make_preview_server, make_server

if TYPE_CHECKING:
    from collections.abc import Iterator

KEY = "test-device-key"
NOW = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
STATE = GaugeState(tank_gal=30.0, range_mi=450.0, updated=NOW)


@pytest.fixture
def base_url() -> Iterator[str]:
    server = make_server(
        host="127.0.0.1",
        port=0,
        api_key=KEY,
        state_fn=lambda: STATE,
        refresh_s=60,
        full_refresh_s=600,
        tz="UTC",
        now_fn=lambda: NOW,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _get(url: str, key: str | None = KEY) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url)
    if key is not None:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_raw_frame_with_refresh_headers(base_url: str) -> None:
    status, headers, body = _get(f"{base_url}/api/display.raw")
    assert status == 200
    assert len(body) == FRAME_BYTES
    assert headers["X-Refresh-Seconds"] == "60"
    assert headers["X-Full-Refresh-Seconds"] == "600"
    assert headers["Cache-Control"] == "no-store"


def test_missing_or_wrong_key_is_rejected(base_url: str) -> None:
    assert _get(f"{base_url}/api/display.raw", key=None)[0] == 401
    assert _get(f"{base_url}/api/display.raw", key="wrong")[0] == 401


def test_unknown_screen_falls_back_to_fuel(base_url: str) -> None:
    """The device may remember HelmLog's screen id ('numbers') from the boat."""
    status, _, body = _get(f"{base_url}/api/display.raw?screen=numbers")
    assert status == 200
    assert body == _get(f"{base_url}/api/display.raw")[2]


def test_screen_query_selects_the_screen(base_url: str) -> None:
    frames = {_get(f"{base_url}/api/display.raw?screen={s}")[2] for s, _ in SCREENS}
    assert len(frames) == len(SCREENS)
    assert all(len(f) == FRAME_BYTES for f in frames)


def test_invert_query_is_honoured(base_url: str) -> None:
    plain = _get(f"{base_url}/api/display.raw")[2]
    inverted = _get(f"{base_url}/api/display.raw?invert=1")[2]
    assert bytes(b ^ 0xFF for b in plain) == inverted


def test_png_preview(base_url: str) -> None:
    status, headers, body = _get(f"{base_url}/api/display.png")
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_touch_menu_regions(base_url: str) -> None:
    """Firmware contract: JSON array of {x, y, w, h, id, name} in 960x540 space."""
    status, headers, body = _get(f"{base_url}/api/display/menu")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    regions = json.loads(body)
    assert [r["id"] for r in regions] == [s for s, _ in SCREENS]
    assert set(regions[0]) == {"x", "y", "w", "h", "id", "name"}


def test_touch_menu_frame(base_url: str) -> None:
    status, _, body = _get(f"{base_url}/api/display/menu.raw")
    assert status == 200
    assert len(body) == FRAME_BYTES


def test_touch_menu_needs_the_key(base_url: str) -> None:
    assert _get(f"{base_url}/api/display/menu", key=None)[0] == 401
    assert _get(f"{base_url}/api/display/menu.raw", key=None)[0] == 401


def test_health_needs_no_key(base_url: str) -> None:
    assert _get(f"{base_url}/health", key=None)[0] == 200


def test_access_log_names_the_client_and_request(base_url: str) -> None:
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG", format="{message}")
    try:
        _get(base_url + "/health", key=None)
    finally:
        logger.remove(sink)
    (line,) = [m for m in lines if "display http" in m]
    assert "127.0.0.1" in line
    assert '"GET /health HTTP/1.1" 200' in line
    assert "%s" not in line


# --- browser preview (tailnet only, via tailscale serve) ---------------------


@pytest.fixture
def preview_url() -> Iterator[str]:
    server = make_preview_server(
        host="127.0.0.1", port=0, state_fn=lambda: STATE, tz="UTC", now_fn=lambda: NOW
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_preview_page_needs_no_key(preview_url: str) -> None:
    status, headers, body = _get(preview_url + "/", key=None)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    for screen, _ in SCREENS:
        assert f'data-screen="{screen}"'.encode() in body


@pytest.mark.parametrize("screen", [s for s, _ in SCREENS])
def test_preview_png_per_screen(preview_url: str, screen: str) -> None:
    status, headers, body = _get(f"{preview_url}/api/display.png?screen={screen}", key=None)
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_preview_menu(preview_url: str) -> None:
    assert _get(f"{preview_url}/api/display/menu.png", key=None)[0] == 200
    regions = json.loads(_get(f"{preview_url}/api/display/menu", key=None)[2])
    assert len(regions) == len(SCREENS)


def test_preview_does_not_serve_device_frames(preview_url: str) -> None:
    """Read-only previews only; the device keeps using the keyed server."""
    assert _get(f"{preview_url}/api/display.raw", key=None)[0] == 404
