"""Dash fuel gauge rendering for the LilyGo T5 S3 PRO (eink-screen firmware contract)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image, ImageStat

from bigair.display_render import (
    FRAME_BYTES,
    HEIGHT,
    WIDTH,
    GaugeState,
    is_stale,
    pack_epd_4bit,
    render_gauge,
)

NOW = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)


def _white() -> Image.Image:
    return Image.new("L", (WIDTH, HEIGHT), 255)


# --- packing: must match eink-screen's framebuffer byte-for-byte ------------


def test_frame_is_exactly_the_firmware_buffer_size() -> None:
    assert FRAME_BYTES == 259_200
    assert len(pack_epd_4bit(_white())) == FRAME_BYTES


def test_white_packs_to_all_ones() -> None:
    assert set(pack_epd_4bit(_white())) == {0xFF}


def test_even_x_pixel_is_the_low_nibble() -> None:
    img = _white()
    img.putpixel((0, 0), 0)
    assert pack_epd_4bit(img)[0] == 0xF0


def test_odd_x_pixel_is_the_high_nibble() -> None:
    img = _white()
    img.putpixel((1, 0), 0)
    assert pack_epd_4bit(img)[0] == 0x0F


def test_grey_uses_the_top_nibble_and_rows_are_row_major() -> None:
    img = _white()
    img.putpixel((2, 1), 0x80)  # row 1, byte 1 of that row, low nibble
    assert pack_epd_4bit(img)[WIDTH // 2 + 1] == 0xF8


def test_invert_flips_black_and_white() -> None:
    assert set(pack_epd_4bit(_white(), invert=True)) == {0x00}


def test_wrong_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="960x540"):
        pack_epd_4bit(Image.new("L", (100, 100), 255))


# --- staleness: a wrong fuel number is worse than no number -----------------


def test_no_data_is_stale() -> None:
    assert is_stale(GaugeState(tank_gal=None, range_mi=None), NOW)


def test_fresh_data_is_not_stale() -> None:
    state = GaugeState(tank_gal=30.0, range_mi=450.0, updated=NOW - timedelta(seconds=30))
    assert not is_stale(state, NOW)


def test_old_data_is_stale() -> None:
    state = GaugeState(tank_gal=30.0, range_mi=450.0, updated=NOW - timedelta(minutes=6))
    assert is_stale(state, NOW)


def test_missing_timestamp_is_stale() -> None:
    assert is_stale(GaugeState(tank_gal=30.0, range_mi=450.0, updated=None), NOW)


# --- rendering ---------------------------------------------------------------


def _fresh(tank: float) -> GaugeState:
    return GaugeState(tank_gal=tank, range_mi=tank * 15, mpg_spark=(14.0, 15.5, 16.0), updated=NOW)


def test_render_is_a_greyscale_frame_of_panel_size() -> None:
    img = render_gauge(_fresh(30.0), NOW, tz="America/Denver")
    assert img.mode == "L"
    assert img.size == (WIDTH, HEIGHT)


def test_stale_and_fresh_frames_differ() -> None:
    fresh = render_gauge(_fresh(30.0), NOW, tz="UTC").tobytes()
    stale = render_gauge(GaugeState(tank_gal=None, range_mi=None), NOW, tz="UTC").tobytes()
    assert fresh != stale


def test_low_fuel_is_drawn_inverted() -> None:
    """Low fuel: the fuel band is white-on-black, so the frame is much darker."""
    normal = ImageStat.Stat(render_gauge(_fresh(30.0), NOW, tz="UTC")).mean[0]
    low = ImageStat.Stat(render_gauge(_fresh(5.0), NOW, tz="UTC")).mean[0]
    assert low < normal - 40
