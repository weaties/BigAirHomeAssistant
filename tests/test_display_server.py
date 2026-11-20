"""HTTP contract served to the eink-screen firmware (same as HelmLog's /api/display.raw)."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import pytest

from bigair.display_render import FRAME_BYTES, GaugeState
from bigair.display_server import make_server

if TYPE_CHECKING:
    from collections.abc import Iterator

KEY = "test-device-key"


@pytest.fixture
def base_url() -> Iterator[str]:
    server = make_server(
        host="127.0.0.1",
        port=0,
        api_key=KEY,
        state_fn=lambda: GaugeState(tank_gal=None, range_mi=None),
        refresh_s=60,
        full_refresh_s=600,
        tz="UTC",
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


def test_unknown_screen_is_ignored_not_404(base_url: str) -> None:
    """The device remembers HelmLog's screen id ('numbers'); BigAir has one screen."""
    status, _, body = _get(f"{base_url}/api/display.raw?screen=numbers")
    assert status == 200
    assert len(body) == FRAME_BYTES


def test_invert_query_is_honoured(base_url: str) -> None:
    plain = _get(f"{base_url}/api/display.raw")[2]
    inverted = _get(f"{base_url}/api/display.raw?invert=1")[2]
    assert bytes(b ^ 0xFF for b in plain) == inverted


def test_png_preview(base_url: str) -> None:
    status, headers, body = _get(f"{base_url}/api/display.png")
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_touch_menu_is_not_offered(base_url: str) -> None:
    assert _get(f"{base_url}/api/display/menu")[0] == 404


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
