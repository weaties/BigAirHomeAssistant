"""Touch-selectable dash screens and the menu (#62)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.display_render import (
    DEFAULT_SCREEN,
    HEIGHT,
    SCREENS,
    WIDTH,
    DebugInfo,
    GaugeState,
    gauge_from_dict,
    menu_regions,
    render_menu,
    render_screen,
    trip_lines,
    van_rows,
)

NOW = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)


def _state(**kw: object) -> GaugeState:
    base: dict[str, object] = {
        "tank_gal": 30.0,
        "range_mi": 450.0,
        "mpg_spark": (14.0, 15.5, 16.0),
        "updated": NOW,
    }
    base.update(kw)
    return GaugeState(**base)  # type: ignore[arg-type]


def test_screen_ids() -> None:
    assert [s for s, _ in SCREENS] == ["fuel", "trip", "economy", "van"]
    assert DEFAULT_SCREEN == "fuel"


@pytest.mark.parametrize("screen", [s for s, _ in SCREENS])
def test_every_screen_renders_a_panel_frame(screen: str) -> None:
    img = render_screen(screen, _state(), NOW, tz="UTC")
    assert img.mode == "L"
    assert img.size == (WIDTH, HEIGHT)


def test_screens_differ() -> None:
    frames = {render_screen(s, _state(), NOW, tz="UTC").tobytes() for s, _ in SCREENS}
    assert len(frames) == len(SCREENS)


@pytest.mark.parametrize("unknown", ["", "numbers", "nope"])
def test_unknown_screen_falls_back_to_fuel(unknown: str) -> None:
    """The device may remember a HelmLog screen id from the boat."""
    fuel = render_screen("fuel", _state(), NOW, tz="UTC").tobytes()
    assert render_screen(unknown, _state(), NOW, tz="UTC").tobytes() == fuel


def test_feed_parses_the_screen_fields() -> None:
    s = gauge_from_dict(
        {
            "ts": "2026-11-21T18:00:00Z",
            "trip_mi": 12.5,
            "trip_gal": 0.9,
            "trip_start": "2026-11-21T17:00:00Z",
            "obd_ts": "2026-11-21T17:59:58Z",
            "ignition": True,
            "gps_ts": "2026-11-21T17:59:59Z",
            "gps_hdop": 1.2,
            "gps_lat": 45.12345,
            "gps_lon": -110.98765,
        }
    )
    assert s.trip_mi == 12.5 and s.trip_gal == 0.9
    assert s.trip_start == NOW - timedelta(hours=1)
    assert s.obd_updated == NOW - timedelta(seconds=2)
    assert s.ignition is True
    assert s.gps_updated == NOW - timedelta(seconds=1)
    assert s.gps_hdop == 1.2
    assert s.gps_lat == 45.12345 and s.gps_lon == -110.98765


def test_feed_without_screen_fields_still_parses() -> None:
    s = gauge_from_dict({"ts": "2026-11-21T18:00:00Z"})
    assert s.trip_start is None and s.obd_updated is None and s.ignition is None


def test_trip_lines() -> None:
    s = _state(trip_mpg=14.2, trip_mi=12.34, trip_gal=0.87, trip_start=NOW - timedelta(minutes=65))
    assert trip_lines(s, NOW) == ("14.2", "12.3 mi", "0.87 gal", "1h05m")


def test_trip_lines_without_a_trip() -> None:
    assert trip_lines(_state(), NOW) == ("--", "0.0 mi", "0.00 gal", "--")


def _rows(state: GaugeState, debug: DebugInfo | None = None) -> dict[str, str]:
    return dict(van_rows(state, debug, NOW, tz="UTC"))


def test_van_rows_live() -> None:
    rows = _rows(
        _state(
            van_battery_v=14.1,
            obd_updated=NOW - timedelta(seconds=3),
            ignition=True,
            gps_updated=NOW - timedelta(seconds=1),
            gps_hdop=0.9,
            gps_lat=45.12345,
            gps_lon=-110.98765,
        ),
        DebugInfo(updated=NOW, pi_5v_v=5.08, uptime_s=3900),
    )
    assert rows["Van battery"] == "14.1 V"
    assert rows["Engine"] == "ON"
    assert rows["GPS"] == "45.123, -110.988 · HDOP 0.9"
    assert rows["Fuel service"] == "LIVE"
    assert rows["Pi supply"] == "5.08 V OK"
    assert rows["Pi uptime"] == "1h05m"


def test_van_rows_when_inputs_are_missing_or_old() -> None:
    rows = _rows(
        _state(
            updated=NOW - timedelta(minutes=10),
            obd_updated=NOW - timedelta(minutes=12),
            ignition=True,
            gps_updated=NOW - timedelta(minutes=11),
        )
    )
    assert rows["Van battery"] == "--"
    assert rows["Engine"] == "NO DATA since 17:48"  # never claims ON from an old sample
    assert rows["GPS"] == "NO FIX since 17:49"
    assert rows["Fuel service"] == "DOWN since 17:50"
    assert rows["Pi supply"] == "--"


def test_van_rows_engine_off_and_never_seen() -> None:
    off = _rows(_state(obd_updated=NOW - timedelta(seconds=5), ignition=False))
    assert off["Engine"] == "OFF"
    never = _rows(GaugeState(tank_gal=None, range_mi=None))
    assert never["Engine"] == "NO DATA"
    assert never["GPS"] == "NO GPS"
    assert never["Fuel service"] == "NO FEED"


def test_van_gps_row_without_position_or_hdop() -> None:
    fresh = NOW - timedelta(seconds=1)
    assert _rows(_state(gps_updated=fresh))["GPS"] == "FIX"
    assert _rows(_state(gps_updated=fresh, gps_lat=1.0, gps_lon=2.0))["GPS"] == "1.000, 2.000"


def test_van_rows_flag_undervoltage() -> None:
    rows = _rows(_state(), DebugInfo(updated=NOW, pi_5v_v=4.72, undervolt_now=True))
    assert rows["Pi supply"] == "4.72 V UNDERVOLT!"


def test_menu_regions_cover_every_screen_inside_the_panel() -> None:
    regions = menu_regions()
    assert [r["id"] for r in regions] == [s for s, _ in SCREENS]
    boxes = []
    for r in regions:
        x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
        assert w >= 200 and h >= 150  # big enough to hit with a finger
        assert x >= 0 and x + w <= WIDTH and y >= 0 and y + h <= HEIGHT
        boxes.append((x, y, x + w, y + h))
    for i, a in enumerate(boxes):  # no overlaps
        for b in boxes[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


def test_menu_image_is_a_panel_frame() -> None:
    img = render_menu()
    assert img.mode == "L" and img.size == (WIDTH, HEIGHT)
