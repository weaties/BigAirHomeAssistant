"""Dash fuel gauge frame for the LilyGo T5 ePaper S3 PRO.

The device runs the eink-screen firmware (~/src/eink-screen): a thin client
that fetches a pre-rendered 960x540 frame and blits it (CLAUDE.md Decision
#29). All layout lives here, so restyling never needs a reflash.

Pure functions: state + time in, image/bytes out. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 960, 540
FRAME_BYTES = WIDTH * HEIGHT // 2  # 259200, the firmware's EPD_FB_SIZE
STALE_AFTER = timedelta(minutes=5)
BLACK, WHITE, GREY = 0, 255, 160


@dataclass(frozen=True)
class GaugeState:
    tank_gal: float | None
    range_mi: float | None
    capacity_gal: float = 47.0
    mpg_spark: tuple[float, ...] = ()  # current-trip mpg buckets, oldest first
    updated: datetime | None = None  # UTC time of the fuel-model state


DEBUG_STALE_AFTER = timedelta(seconds=30)


@dataclass(frozen=True)
class DebugInfo:
    """Power debug readings for drive testing (Pi supply, van battery)."""

    updated: datetime  # UTC
    pi_5v_v: float | None = None
    undervolt_now: bool = False
    undervolt_since_boot: bool = False
    throttled_since_boot: bool = False
    van_battery_v: float | None = None
    uptime_s: float | None = None


def debug_from_dict(d: dict[str, Any]) -> DebugInfo:
    """Parse the host's debug JSON (deploy/host/bigair-debug.py)."""
    try:
        updated = datetime.strptime(str(d["ts"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (KeyError, ValueError) as e:
        raise ValueError(f"bad debug record: {e}") from e
    flags = int(str(d.get("throttled", "0")), 16)  # vcgencmd get_throttled bits

    def num(key: str) -> float | None:
        v = d.get(key)
        return None if v is None else float(v)

    return DebugInfo(
        updated=updated,
        pi_5v_v=num("pi_5v_v"),
        undervolt_now=bool(flags & 0x1),
        undervolt_since_boot=bool(flags & 0x10000),
        throttled_since_boot=bool(flags & 0x40000),
        van_battery_v=num("van_battery_v"),
        uptime_s=num("uptime_s"),
    )


def debug_text(info: DebugInfo, now: datetime) -> str:
    """One status line, e.g. "PI 5.01V OK | VAN 14.2V | UP 12m"."""
    age = (now - info.updated).total_seconds()
    prefix = f"DEBUG STALE {age:.0f}s | " if age > DEBUG_STALE_AFTER.total_seconds() else ""
    pi = "--" if info.pi_5v_v is None else f"{info.pi_5v_v:.2f}"
    if info.undervolt_now:
        health = "UNDERVOLT!"
    else:
        past = [
            n
            for n, f in (("UV", info.undervolt_since_boot), ("THR", info.throttled_since_boot))
            if f
        ]
        health = f"({','.join(past)} since boot)" if past else "OK"
    van = "--" if info.van_battery_v is None else f"{info.van_battery_v:.1f}"
    if info.uptime_s is None:
        up = "--"
    else:
        mins = int(info.uptime_s // 60)
        up = f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"
    return f"{prefix}PI {pi}V {health} | VAN {van}V | UP {up}"


def is_stale(state: GaugeState, now: datetime, max_age: timedelta = STALE_AFTER) -> bool:
    """A missing, untimed or old reading is stale; never shown as current."""
    if state.tank_gal is None or state.range_mi is None or state.updated is None:
        return True
    return now - state.updated > max_age


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _local(ts: datetime, tz: str) -> str:
    return ts.astimezone(ZoneInfo(tz)).strftime("%H:%M")


def _sparkline(
    draw: ImageDraw.ImageDraw, values: tuple[float, ...], box: tuple[int, int, int, int]
) -> None:
    x0, y0, x1, y1 = box
    draw.line([(x0, y1), (x1, y1)], fill=GREY, width=2)
    if len(values) < 2:
        draw.text((x0, y0 + 20), "trip mpg: not enough data yet", font=_font(28), fill=GREY)
        return
    lo = max(min(values) - 2.0, 0.0)  # scale to this trip's own range
    hi = max(values) + 2.0
    step = (x1 - x0) / (len(values) - 1)
    pts = [(x0 + i * step, y1 - (v - lo) / (hi - lo) * (y1 - y0)) for i, v in enumerate(values)]
    draw.text((x1 - 300, y0 - 34), f"trip {values[-1]:.1f} mpg", font=_font(28), fill=BLACK)
    draw.line(pts, fill=BLACK, width=5, joint="curve")


def render_gauge(
    state: GaugeState,
    now: datetime,
    *,
    tz: str,
    low_fuel_gal: float = 8.0,
    debug: DebugInfo | None = None,
) -> Image.Image:
    """Render the fuel gauge: tank gallons + level bar, range, trip-mpg sparkline, status."""
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)
    stale = is_stale(state, now)
    low = not stale and state.tank_gal is not None and state.tank_gal < low_fuel_gal

    # Fuel band. Low fuel is drawn inverted (B/W panel has no warning colour).
    ink = WHITE if low else BLACK
    if low:
        draw.rectangle((0, 0, WIDTH, 250), fill=BLACK)
    tank = "--" if state.tank_gal is None else f"{state.tank_gal:.1f}"
    draw.text((40, 30), tank, font=_font(170), fill=ink)
    draw.text(
        (40 + draw.textlength(tank, font=_font(170)) + 20, 140), "gal", font=_font(60), fill=ink
    )

    # Level bar.
    bx0, by0, bx1, by1 = 640, 70, 920, 180
    draw.rectangle((bx0, by0, bx1, by1), outline=ink, width=5)
    if state.tank_gal is not None:
        frac = min(max(state.tank_gal / state.capacity_gal, 0.0), 1.0)
        draw.rectangle(
            (bx0 + 8, by0 + 8, bx0 + 8 + int((bx1 - bx0 - 16) * frac), by1 - 8), fill=ink
        )
        draw.text(
            (bx0, by1 + 10), f"{frac:.0%} of {state.capacity_gal:.0f}", font=_font(32), fill=ink
        )

    # Range.
    rng = "--" if state.range_mi is None else f"{state.range_mi:.0f}"
    draw.text((40, 265), f"{rng} mi range", font=_font(80), fill=BLACK)

    # Stale: strike through the numbers so an old value never reads as current.
    if stale and state.tank_gal is not None:
        draw.line([(30, 120), (600, 120)], fill=ink, width=8)
        draw.line([(30, 310), (700, 310)], fill=BLACK, width=8)

    if debug is None:
        _sparkline(draw, state.mpg_spark, (40, 380, 920, 480))
    else:
        # Drive-testing mode: the debug strip takes the sparkline's place.
        draw.rectangle((20, 375, WIDTH - 20, 485), outline=BLACK, width=3)
        draw.text((40, 395), debug_text(debug, now), font=_font(40), fill=BLACK)
        draw.text((40, 445), f"debug @ {_local(debug.updated, tz)}", font=_font(28), fill=GREY)

    # Footer: local clock + status.
    draw.line([(0, 495), (WIDTH, 495)], fill=GREY, width=2)
    draw.text((40, 502), _local(now, tz), font=_font(32), fill=BLACK)
    if state.updated is None:
        status = "NO FUEL DATA"
    elif stale:
        status = f"STALE since {_local(state.updated, tz)}"
    else:
        status = "LIVE"
    draw.text(
        (WIDTH - 40 - draw.textlength(status, font=_font(32)), 502),
        status,
        font=_font(32),
        fill=BLACK,
    )
    return img


def pack_epd_4bit(img: Image.Image, *, invert: bool = False) -> bytes:
    """Pack an "L" image into the panel's 4-bit framebuffer (eink-screen / HelmLog layout).

    Top nibble of each grey value; even-x pixel in the low nibble, odd-x in the
    high nibble; rows in order. 0x0 black, 0xF white.
    """
    if img.mode != "L":
        img = img.convert("L")
    if img.size != (WIDTH, HEIGHT):
        raise ValueError(f"expected {WIDTH}x{HEIGHT} image, got {img.size[0]}x{img.size[1]}")
    arr = np.frombuffer(img.tobytes(), dtype=np.uint8).reshape(HEIGHT, WIDTH)
    if invert:
        arr = 255 - arr
    nib = arr >> 4
    packed = (nib[:, 1::2] << 4) | nib[:, 0::2]
    return packed.astype(np.uint8).tobytes()
