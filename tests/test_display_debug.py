"""Debug strip on the dash gauge: Pi supply and van voltage while testing power."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.display_render import (
    DebugInfo,
    GaugeState,
    debug_from_dict,
    debug_text,
    render_gauge,
)

NOW = datetime(2026, 11, 20, 17, 30, tzinfo=UTC)


def _info(**kw: object) -> DebugInfo:
    base: dict[str, object] = {
        "updated": NOW - timedelta(seconds=3),
        "pi_5v_v": 5.01,
        "undervolt_now": False,
        "undervolt_since_boot": False,
        "throttled_since_boot": False,
        "van_battery_v": 14.2,
        "uptime_s": 754,
    }
    base.update(kw)
    return DebugInfo(**base)  # type: ignore[arg-type]


def test_healthy_line() -> None:
    assert debug_text(_info(), NOW) == "PI 5.01V OK | VAN 14.2V | UP 12m"


def test_undervolt_now_is_shouted() -> None:
    assert debug_text(_info(pi_5v_v=4.71, undervolt_now=True), NOW).startswith(
        "PI 4.71V UNDERVOLT!"
    )


def test_past_undervolt_and_throttle_are_flagged() -> None:
    line = debug_text(_info(undervolt_since_boot=True, throttled_since_boot=True), NOW)
    assert "PI 5.01V (UV,THR since boot)" in line


def test_missing_values_show_dashes() -> None:
    line = debug_text(_info(pi_5v_v=None, van_battery_v=None, uptime_s=None), NOW)
    assert line == "PI --V OK | VAN --V | UP --"


def test_uptime_in_hours() -> None:
    assert debug_text(_info(uptime_s=2 * 3600 + 5 * 60), NOW).endswith("UP 2h05m")


def test_old_debug_data_says_so() -> None:
    line = debug_text(_info(updated=NOW - timedelta(seconds=45)), NOW)
    assert line.startswith("DEBUG STALE 45s")


def test_from_dict_parses_the_host_file() -> None:
    info = debug_from_dict(
        {
            "ts": "2026-11-20T17:29:57Z",
            "pi_5v_v": 5.01,
            "throttled": "0x50000",
            "van_battery_v": 14.2,
            "uptime_s": 754,
        }
    )
    assert info.updated == NOW - timedelta(seconds=3)
    assert info.undervolt_since_boot and info.throttled_since_boot
    assert not info.undervolt_now


def test_from_dict_reads_undervolt_now_from_bit_0() -> None:
    info = debug_from_dict({"ts": "2026-11-20T17:29:57Z", "throttled": "0x50005"})
    assert info.undervolt_now
    assert info.pi_5v_v is None and info.van_battery_v is None


@pytest.mark.parametrize("bad", [{}, {"ts": "nope"}])
def test_from_dict_rejects_garbage(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        debug_from_dict(bad)


def test_debug_strip_changes_the_frame() -> None:
    state = GaugeState(tank_gal=None, range_mi=None)
    plain = render_gauge(state, NOW, tz="UTC")
    with_debug = render_gauge(state, NOW, tz="UTC", debug=_info())
    assert plain.tobytes() != with_debug.tobytes()


def test_file_reader_returns_none_without_a_usable_file(tmp_path: object) -> None:
    from pathlib import Path

    from bigair.display_server import debug_file_reader

    path = Path(str(tmp_path)) / "debug.json"
    read = debug_file_reader(path)
    assert read() is None  # missing
    path.write_text("{garbage")
    assert read() is None  # unparseable
    path.write_text('{"ts": "2026-11-20T17:29:57Z", "pi_5v_v": 5.0, "throttled": "0x0"}')
    info = read()
    assert info is not None and info.pi_5v_v == 5.0


def test_longest_debug_line_fits_the_panel() -> None:
    from PIL import Image, ImageDraw

    from bigair.display_render import WIDTH, fit_font

    worst = debug_text(
        _info(
            updated=NOW - timedelta(seconds=999),
            pi_5v_v=4.71,
            undervolt_now=True,
            van_battery_v=12.9,
            uptime_s=99 * 3600,
        ),
        NOW,
    )
    draw = ImageDraw.Draw(Image.new("L", (WIDTH, 100)))
    font = fit_font(draw, worst, max_width=WIDTH - 80, start=40)
    assert draw.textlength(worst, font=font) <= WIDTH - 80
