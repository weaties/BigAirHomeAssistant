"""Dash fuel gauge frame for the LilyGo T5 ePaper S3 PRO.

The device runs the eink-screen firmware (~/src/eink-screen): a thin client
that fetches a pre-rendered 960x540 frame and blits it (CLAUDE.md Decision
#29). All layout lives here, so restyling never needs a reflash.

Screens (#62): the firmware's touch menu fetches `menu_regions()` and the
`render_menu()` frame, then asks for frames with `?screen=<id>`. Ids are in
SCREENS; anything else renders the fuel screen.

Pure functions: state + time in, image/bytes out. No I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 960, 540
FRAME_BYTES = WIDTH * HEIGHT // 2  # 259200, the firmware's EPD_FB_SIZE
STALE_AFTER = timedelta(minutes=5)
BLACK, WHITE, GREY = 0, 255, 160
FOOTER_Y = 478  # footer rule; content stays above it


@dataclass(frozen=True)
class GaugeState:
    tank_gal: float | None
    range_mi: float | None
    capacity_gal: float = 47.0
    mpg_spark: tuple[float, ...] = ()  # current-trip mpg buckets, oldest first
    updated: datetime | None = None  # UTC time the fuel service wrote the feed
    stale: bool = False  # the service says its inputs stopped (moving, no data)
    calibrating: bool = False  # 5E not yet VERIFIED (Decision #32)
    van_battery_v: float | None = None  # WiCAN ATRV, fresh only (debug strip)
    mpg_rolling: float | None = None  # 150 mi average; None until measured (not the prior)
    mpg_instant: float | None = None  # trailing 30 s
    mpg_since_full: float | None = None  # last full-to-full interval
    trip_mpg: float | None = None  # current trip average
    fill_pending: bool = False  # a detected fill awaits Dan's answer (#11, Decision #34)
    trip_mi: float = 0.0  # current trip (#62)
    trip_gal: float = 0.0
    trip_start: datetime | None = None  # UTC
    obd_updated: datetime | None = None  # UTC time of the last WiCAN sample
    ignition: bool | None = None  # engine ECU answering in that sample
    gps_updated: datetime | None = None  # UTC time of the last GPS fix
    gps_hdop: float | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None


def gauge_from_dict(d: dict[str, Any]) -> GaugeState:
    """Parse the fuel service's gauge feed (FuelService.gauge_feed)."""

    def num(key: str) -> float | None:
        v = d.get(key)
        return None if v is None else float(v)

    def when(key: str) -> datetime | None:
        v = d.get(key)
        return None if v is None else _parse_ts(str(v))

    try:
        updated = _parse_ts(str(d["ts"]))
    except (KeyError, ValueError) as e:
        raise ValueError(f"bad gauge feed: {e}") from e
    ignition = d.get("ignition")
    return GaugeState(
        tank_gal=num("tank_gal"),
        range_mi=num("range_mi"),
        capacity_gal=num("capacity_gal") or 47.0,
        mpg_spark=tuple(float(v) for v in d.get("mpg_spark") or ()),
        updated=updated,
        stale=bool(d.get("stale", False)),
        calibrating=bool(d.get("calibrating", False)),
        van_battery_v=num("van_battery_v"),
        mpg_rolling=num("mpg_rolling"),
        mpg_instant=num("mpg_instant_30s"),
        mpg_since_full=num("mpg_since_full"),
        trip_mpg=num("trip_mpg"),
        fill_pending=bool(d.get("fill_pending", False)),
        trip_mi=num("trip_mi") or 0.0,
        trip_gal=num("trip_gal") or 0.0,
        trip_start=when("trip_start"),
        obd_updated=when("obd_ts"),
        ignition=None if ignition is None else bool(ignition),
        gps_updated=when("gps_ts"),
        gps_hdop=num("gps_hdop"),
        gps_lat=num("gps_lat"),
        gps_lon=num("gps_lon"),
    )


def _parse_ts(v: str) -> datetime:
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


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


def with_van_voltage(debug: DebugInfo | None, gauge: GaugeState) -> DebugInfo | None:
    """Fill the debug strip's van voltage from the fuel service when the host has none."""
    if debug is None or debug.van_battery_v is not None or gauge.van_battery_v is None:
        return debug
    return replace(debug, van_battery_v=gauge.van_battery_v)


def debug_text(info: DebugInfo, now: datetime) -> str:
    """One status line, e.g. "PI 5.01V OK | VAN 14.2V | UP 12m"."""
    age = (now - info.updated).total_seconds()
    prefix = f"DEBUG STALE {age:.0f}s | " if age > DEBUG_STALE_AFTER.total_seconds() else ""
    pi = "--" if info.pi_5v_v is None else f"{info.pi_5v_v:.2f}"
    van = "--" if info.van_battery_v is None else f"{info.van_battery_v:.1f}"
    up = "--" if info.uptime_s is None else _dur(info.uptime_s)
    return f"{prefix}PI {pi}V {_pi_health(info)} | VAN {van}V | UP {up}"


def _pi_health(info: DebugInfo) -> str:
    if info.undervolt_now:
        return "UNDERVOLT!"
    past = [
        n for n, f in (("UV", info.undervolt_since_boot), ("THR", info.throttled_since_boot)) if f
    ]
    return f"({','.join(past)} since boot)" if past else "OK"


def _dur(seconds: float) -> str:
    """45m, 1h05m."""
    mins = int(seconds // 60)
    return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def is_stale(state: GaugeState, now: datetime, max_age: timedelta = STALE_AFTER) -> bool:
    """A missing, untimed or old reading is stale; never shown as current."""
    if state.tank_gal is None or state.range_mi is None or state.updated is None:
        return True
    return state.stale or now - state.updated > max_age


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def fit_font(
    draw: ImageDraw.ImageDraw, text: str, *, max_width: float, start: int, smallest: int = 20
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Largest font from `start` down whose rendering of `text` fits `max_width`."""
    size = start
    while size > smallest and draw.textlength(text, font=_font(size)) > max_width:
        size -= 2
    return _font(size)


def _local(ts: datetime, tz: str) -> str:
    return ts.astimezone(ZoneInfo(tz)).strftime("%H:%M")


def _sparkline(
    draw: ImageDraw.ImageDraw,
    values: tuple[float, ...],
    box: tuple[int, int, int, int],
    *,
    ink: int = BLACK,
    scale_labels: bool = False,
) -> None:
    x0, y0, x1, y1 = box
    draw.line([(x0, y1), (x1, y1)], fill=GREY, width=2)
    if len(values) < 2:
        draw.text((x0, y0 + 10), "trip mpg: not enough data yet", font=_font(42), fill=GREY)
        return
    lo = max(min(values) - 2.0, 0.0)  # scale to this trip's own range
    hi = max(values) + 2.0
    step = (x1 - x0) / (len(values) - 1)
    pts = [(x0 + i * step, y1 - (v - lo) / (hi - lo) * (y1 - y0)) for i, v in enumerate(values)]
    draw.line(pts, fill=ink, width=5, joint="curve")
    if scale_labels:
        draw.text((x1 + 8, y0 - 6), f"{max(values):.0f}", font=_font(36), fill=GREY)
        draw.text((x1 + 8, y1 - 36), f"{min(values):.0f}", font=_font(36), fill=GREY)


def mpg_lines(state: GaugeState) -> tuple[str, str, str]:
    """(big number, its label, detail line) for the economy block."""

    def f(v: float | None) -> str:
        return "--" if v is None else f"{v:.1f}"

    big = f(state.mpg_rolling)
    label = "150 mi avg" if state.mpg_rolling is not None else "avg after 50 mi"
    detail = (
        f"now {f(state.mpg_instant)} · trip {f(state.trip_mpg)} · fill {f(state.mpg_since_full)}"
    )
    return big, label, detail


def status_text(state: GaugeState, now: datetime, tz: str) -> str:
    """Footer status: no data, stale, or live (with the calibrating disclosure)."""
    if state.updated is None or state.tank_gal is None:
        return "NO FUEL DATA"
    if state.stale:
        return "STALE: NO ENGINE DATA"  # service alive, but samples stopped while moving
    if is_stale(state, now):
        return f"STALE since {_local(state.updated, tz)}"  # feed stopped: service down
    if state.fill_pending:
        return "FILL PENDING · ANSWER ON PHONE"  # tank excludes the unconfirmed fill
    if state.calibrating:
        return "LIVE · CALIBRATING"  # 5E not yet verified against the pump (Decision #32)
    return "LIVE"


def render_gauge(
    state: GaugeState,
    now: datetime,
    *,
    tz: str,
    low_fuel_gal: float = 8.0,
) -> Image.Image:
    """Fuel screen: tank gallons + level bar, range, economy line, trip-mpg sparkline."""
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)
    stale = is_stale(state, now)
    low = not stale and state.tank_gal is not None and state.tank_gal < low_fuel_gal

    # Fuel band. Low fuel is drawn inverted (B/W panel has no warning colour).
    ink = WHITE if low else BLACK
    if low:
        draw.rectangle((0, 0, WIDTH, 255), fill=BLACK)
    tank = "--" if state.tank_gal is None else f"{state.tank_gal:.1f}"
    draw.text((40, 30), tank, font=_font(170), fill=ink)
    draw.text(
        (40 + draw.textlength(tank, font=_font(170)) + 20, 140), "gal", font=_font(60), fill=ink
    )

    # Analog fuel gauge: E at 9 o'clock, F at 3 o'clock, needle at the level.
    _arc_gauge(draw, state, ink)

    # Range.
    rng = "--" if state.range_mi is None else f"{state.range_mi:.0f}"
    draw.text((40, 265), f"{rng} mi range", font=_font(80), fill=BLACK)

    # Economy block (right of the range): measured 150 mi average, then details.
    big, label, detail = mpg_lines(state)
    draw.text((620, 258), big, font=_font(72), fill=BLACK)
    draw.text(
        (620 + draw.textlength(big, font=_font(72)) + 12, 282), "mpg", font=_font(48), fill=BLACK
    )
    draw.text((624, 338), label, font=_font(36), fill=GREY)
    draw.text((40, 352), detail, font=fit_font(draw, detail, max_width=560, start=39), fill=BLACK)

    # Stale: strike through the numbers so an old value never reads as current.
    if stale and state.tank_gal is not None:
        draw.line([(30, 120), (600, 120)], fill=ink, width=8)
        draw.line([(30, 310), (700, 310)], fill=BLACK, width=8)

    _sparkline(draw, state.mpg_spark, (40, 405, 920, 468))
    _footer(draw, state, now, tz, "FUEL")
    return img


def _footer(
    draw: ImageDraw.ImageDraw, state: GaugeState, now: datetime, tz: str, title: str
) -> None:
    """Shared footer: local clock, screen name (when it fits), fuel status."""
    draw.line([(0, FOOTER_Y), (WIDTH, FOOTER_Y)], fill=GREY, width=2)
    y = FOOTER_Y + 6
    clock = _local(now, tz)
    draw.text((30, y), clock, font=_font(48), fill=BLACK)
    clock_end = 30 + draw.textlength(clock, font=_font(48))
    status = status_text(state, now, tz)
    font = fit_font(draw, status, max_width=WIDTH - clock_end - 70, start=48)
    status_x = WIDTH - 30 - draw.textlength(status, font=font)
    draw.text(
        (status_x, y + (48 - int(getattr(font, "size", 48))) // 2), status, font=font, fill=BLACK
    )
    title_w = draw.textlength(title, font=_font(42))
    title_x = (WIDTH - title_w) / 2
    if title_x > clock_end + 24 and title_x + title_w < status_x - 24:
        draw.text((title_x, y + 4), title, font=_font(42), fill=GREY)


def needle_angle(frac: float) -> float:
    """PIL angle for a tank fraction: 180 (E, 9 o'clock) through 270 (top) to 360 (F)."""
    return 180.0 + 180.0 * min(max(frac, 0.0), 1.0)


ARC_CX, ARC_CY, ARC_R = 780, 192, 150


def _arc_gauge(draw: ImageDraw.ImageDraw, state: GaugeState, ink: int) -> None:
    cx, cy, r = ARC_CX, ARC_CY, ARC_R
    draw.arc((cx - r, cy - r, cx + r, cy + r), 180, 360, fill=ink, width=6)  # the dial
    for i in range(5):  # E, 1/4, 1/2, 3/4, F
        a = math.radians(needle_angle(i / 4))
        inner = r - (34 if i % 2 == 0 else 22)
        draw.line(
            [
                (cx + inner * math.cos(a), cy + inner * math.sin(a)),
                (cx + r * math.cos(a), cy + r * math.sin(a)),
            ],
            fill=ink,
            width=6,
        )
    draw.text((cx - r - 6, cy + 6), "E", font=_font(42), fill=ink)
    draw.text((cx + r - 22, cy + 6), "F", font=_font(42), fill=ink)
    hub = 14
    if state.tank_gal is None:
        draw.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), outline=ink, width=4)
        return  # no needle without a level: never a confident guess
    frac = state.tank_gal / state.capacity_gal
    a = math.radians(needle_angle(frac))
    tip = r - 16
    draw.line([(cx, cy), (cx + tip * math.cos(a), cy + tip * math.sin(a))], fill=ink, width=10)
    draw.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=ink)
    pct = f"{min(max(frac, 0.0), 1.0):.0%}"
    draw.text(
        (cx - draw.textlength(pct, font=_font(36)) / 2, cy + 18), pct, font=_font(36), fill=ink
    )


def _num(v: float | None, fmt: str = ".1f") -> str:
    return "--" if v is None else format(v, fmt)


def trip_lines(state: GaugeState, now: datetime) -> tuple[str, str, str, str]:
    """(trip mpg, miles, gallons, elapsed) for the trip screen."""
    elapsed = "--" if state.trip_start is None else _dur((now - state.trip_start).total_seconds())
    return (
        _num(state.trip_mpg),
        f"{state.trip_mi:.1f} mi",
        f"{state.trip_gal:.2f} gal",
        elapsed,
    )


def render_trip(state: GaugeState, now: datetime, *, tz: str) -> Image.Image:
    """Trip screen: trip mpg, miles, fuel, elapsed time, sparkline."""
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)
    ink = GREY if is_stale(state, now) else BLACK  # an old feed never reads as current
    mpg, miles, gal, elapsed = trip_lines(state, now)
    draw.text((40, 30), mpg, font=_font(170), fill=ink)
    draw.text(
        (40 + draw.textlength(mpg, font=_font(170)) + 20, 140), "mpg", font=_font(60), fill=ink
    )
    draw.text((44, 212), "trip average", font=_font(48), fill=GREY)
    for i, (value, label) in enumerate(
        ((miles, "driven"), (gal, "used"), (elapsed, "since start"))
    ):
        y = 14 + i * 100
        draw.text((600, y), value, font=_font(56), fill=ink)
        draw.text((604, y + 58), label, font=_font(33), fill=GREY)
    draw.text((40, 290), "trip mpg, 2 mi buckets", font=_font(36), fill=GREY)
    _sparkline(draw, state.mpg_spark, (40, 345, 870, 466), ink=ink, scale_labels=True)
    _footer(draw, state, now, tz, "TRIP")
    return img


def render_economy(state: GaugeState, now: datetime, *, tz: str) -> Image.Image:
    """Economy screen: 150 mi average, instant / trip / since-full, large sparkline."""
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)
    ink = GREY if is_stale(state, now) else BLACK
    big, label, _ = mpg_lines(state)
    draw.text((40, 30), big, font=_font(170), fill=ink)
    draw.text(
        (40 + draw.textlength(big, font=_font(170)) + 20, 140), "mpg", font=_font(60), fill=ink
    )
    draw.text((44, 212), label, font=_font(48), fill=GREY)
    rows = (
        (state.mpg_instant, "last 30 s"),
        (state.trip_mpg, "this trip"),
        (state.mpg_since_full, "last full-to-full"),
    )
    for i, (value, row_label) in enumerate(rows):
        y = 14 + i * 100
        draw.text((600, y), _num(value), font=_font(56), fill=ink)
        draw.text((604, y + 58), row_label, font=_font(33), fill=GREY)
    draw.text((40, 290), "trip mpg, 2 mi buckets", font=_font(36), fill=GREY)
    _sparkline(draw, state.mpg_spark, (40, 345, 870, 466), ink=ink, scale_labels=True)
    _footer(draw, state, now, tz, "ECONOMY")
    return img


OBD_FRESH = timedelta(seconds=60)
GPS_FRESH = timedelta(seconds=30)


def van_rows(
    state: GaugeState, debug: DebugInfo | None, now: datetime, *, tz: str
) -> list[tuple[str, str]]:
    """(label, value) rows for the van screen. Old inputs say so; nothing is guessed."""
    feed_live = state.updated is not None and now - state.updated <= STALE_AFTER
    volts = state.van_battery_v if feed_live else None
    if volts is None and debug is not None:
        volts = debug.van_battery_v

    if state.obd_updated is None:
        engine = "NO DATA"
    elif now - state.obd_updated > OBD_FRESH:
        engine = f"NO DATA since {_local(state.obd_updated, tz)}"
    else:
        engine = "ON" if state.ignition else "OFF"

    if state.gps_updated is None:
        gps = "NO GPS"
    elif now - state.gps_updated > GPS_FRESH:
        gps = f"NO FIX since {_local(state.gps_updated, tz)}"
    else:
        parts = []
        if state.gps_lat is not None and state.gps_lon is not None:
            parts.append(f"{state.gps_lat:.3f}, {state.gps_lon:.3f}")
        if state.gps_hdop is not None:
            parts.append(f"HDOP {state.gps_hdop:.1f}")
        gps = " · ".join(parts) or "FIX"

    if state.updated is None:
        service = "NO FEED"
    elif not feed_live:
        service = f"DOWN since {_local(state.updated, tz)}"
    else:
        service = "LIVE"

    if debug is None:
        supply = uptime = "--"
    elif now - debug.updated > DEBUG_STALE_AFTER:
        supply = uptime = f"STALE since {_local(debug.updated, tz)}"
    else:
        supply = f"{_num(debug.pi_5v_v, '.2f')} V {_pi_health(debug)}"
        uptime = "--" if debug.uptime_s is None else _dur(debug.uptime_s)

    return [
        ("Van battery", "--" if volts is None else f"{volts:.1f} V"),
        ("Engine", engine),
        ("GPS", gps),
        ("Fuel service", service),
        ("Pi supply", supply),
        ("Pi uptime", uptime),
    ]


def render_van(
    state: GaugeState, now: datetime, *, tz: str, debug: DebugInfo | None = None
) -> Image.Image:
    """Van screen: battery, engine/OBD, GPS, service and Pi supply health."""
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)
    for i, (label, value) in enumerate(van_rows(state, debug, now, tz=tz)):
        y = 10 + i * 78
        draw.text((30, y + 6), label, font=_font(54), fill=GREY)
        font = fit_font(draw, value, max_width=WIDTH - 400 - 30, start=56)
        draw.text((400, y + 4), value, font=font, fill=BLACK)
        if i:
            draw.line([(30, y - 4), (WIDTH - 30, y - 4)], fill=GREY, width=1)
    _footer(draw, state, now, tz, "VAN")
    return img


SCREENS: tuple[tuple[str, str], ...] = (
    ("fuel", "Fuel"),
    ("trip", "Trip"),
    ("economy", "Economy"),
    ("van", "Van"),
)
DEFAULT_SCREEN = "fuel"
_SCREEN_HINTS = {
    "fuel": "tank and range",
    "trip": "this trip",
    "economy": "mpg detail",
    "van": "power, GPS, OBD",
}


def render_screen(
    screen: str,
    state: GaugeState,
    now: datetime,
    *,
    tz: str,
    debug: DebugInfo | None = None,
) -> Image.Image:
    """Render one screen by id; unknown ids (e.g. a HelmLog id) get the fuel screen."""
    if screen == "trip":
        return render_trip(state, now, tz=tz)
    if screen == "economy":
        return render_economy(state, now, tz=tz)
    if screen == "van":
        return render_van(state, now, tz=tz, debug=debug)
    return render_gauge(state, now, tz=tz)


def menu_regions() -> list[dict[str, int | str]]:
    """Tap regions for the firmware's menu, 2x2 tiles in 960x540 space."""
    regions: list[dict[str, int | str]] = []
    for i, (sid, name) in enumerate(SCREENS):
        col, row = i % 2, i // 2
        regions.append(
            {"x": 30 + col * 470, "y": 100 + row * 215, "w": 430, "h": 195, "id": sid, "name": name}
        )
    return regions


def render_menu() -> Image.Image:
    """The touch menu frame; its tiles match menu_regions()."""
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)
    draw.text((30, 25), "Choose a screen", font=_font(48), fill=BLACK)
    hint = "tap outside to cancel"
    draw.text(
        (WIDTH - 30 - draw.textlength(hint, font=_font(42)), 30), hint, font=_font(42), fill=GREY
    )
    for r in menu_regions():
        x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
        draw.rounded_rectangle((x, y, x + w, y + h), radius=18, outline=BLACK, width=5)
        name, sub = str(r["name"]), _SCREEN_HINTS[str(r["id"])]
        draw.text(
            (x + (w - draw.textlength(name, font=_font(72))) / 2, y + 30),
            name,
            font=_font(72),
            fill=BLACK,
        )
        draw.text(
            (x + (w - draw.textlength(sub, font=_font(42))) / 2, y + 125),
            sub,
            font=_font(42),
            fill=GREY,
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
