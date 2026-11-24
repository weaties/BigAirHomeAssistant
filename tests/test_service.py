"""bigair service core: samples → fuel model → state file / HA payload / Influx (#53)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.service import FuelService, ServiceConfig
from bigair.signals import ObdSample
from bigair.state_file import read_state

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


def _svc(tmp_path: Path, **kw: object) -> FuelService:
    return FuelService.start(ServiceConfig(state_path=tmp_path / "fuel_state.json", **kw))  # type: ignore[arg-type]


def _s(
    sec: float,
    gph: float | None = 3.6,
    mph: float | None = 60.0,
    level: float | None = 50.0,
    ignition: bool = True,
) -> ObdSample:
    return ObdSample(
        ts=T0 + timedelta(seconds=sec),
        ignition=ignition,
        speed_mph=mph,
        fuel_rate_gph=gph,
        fuel_level_pct=level,
    )


def _payload(svc: FuelService, sec: float) -> dict[str, object]:
    return json.loads(svc.state_payload(T0 + timedelta(seconds=sec)))


def test_fresh_install_has_no_tank_until_filled(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_sample(_s(0))
    p = _payload(svc, 0)
    assert p["tank_gal"] is None and p["range_mi"] is None
    assert not (tmp_path / "fuel_state.json").exists()


def test_filled_to_full_seeds_the_tank_and_saves_immediately(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    assert _payload(svc, 0)["tank_gal"] == 47.0
    saved = read_state(tmp_path / "fuel_state.json")
    assert saved is not None and saved["state"]["gal_after_last_fill"] == 47.0


def test_samples_burn_fuel_and_state_is_saved(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    for i in range(1, 3602):
        svc.handle_sample(_s(i))
    p = _payload(svc, 3601)
    assert p["tank_gal"] == pytest.approx(47.0 - 3.6, abs=0.01)
    saved = read_state(tmp_path / "fuel_state.json")
    assert saved is not None
    assert saved["state"]["engine_used_gal"] == pytest.approx(3.6, abs=0.01)


def test_restart_restores_the_tank(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    for i in range(1, 601):
        svc.handle_sample(_s(i))
    svc.flush(T0 + timedelta(seconds=601))
    tank = svc.model.tank_gal if svc.model else None
    again = _svc(tmp_path)
    assert again.model is not None
    assert again.model.tank_gal == pytest.approx(tank)


def test_filled_to_full_again_resets_the_tank(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    for i in range(1, 3602):
        svc.handle_sample(_s(i))
    svc.handle_manual_full(T0 + timedelta(seconds=3700))
    assert _payload(svc, 3700)["tank_gal"] == 47.0


def test_payload_flags_calibrating_until_fuel_rate_is_verified(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    assert _payload(svc, 0)["calibrating"] is True
    verified = _svc(tmp_path, fuel_rate_verified=True)
    assert _payload(verified, 0)["calibrating"] is False


def test_moving_then_silent_is_stale(tmp_path: Path) -> None:
    """Wi-Fi drop while driving: no samples at all. Don't keep showing a confident tank."""
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    svc.handle_sample(_s(1, mph=55.0))
    assert _payload(svc, 30)["stale"] is False
    assert _payload(svc, 62)["stale"] is True


def test_stopped_then_silent_is_parked_not_stale(tmp_path: Path) -> None:
    """Engine off: the WiCAN goes to sleep. The tank is still right."""
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    svc.handle_sample(_s(1, gph=0.6, mph=0.0))
    assert _payload(svc, 3600)["stale"] is False


def test_fill_candidates_come_out_of_the_detector(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    t = 0
    candidates = []
    for _ in range(120):
        t += 1
        svc.handle_sample(_s(t, gph=0.6, mph=0.0, level=30.0))
    for _ in range(600):
        t += 1
        svc.handle_sample(_s(t, gph=None, mph=None, level=None, ignition=False))
    for _ in range(60):
        t += 1
        c = svc.handle_sample(_s(t, gph=0.6, mph=0.0, level=80.0))
        if c is not None:
            candidates.append(c)
    assert len(candidates) == 1
    assert candidates[0].est_gallons == pytest.approx(0.5 * 47.0)


def test_influx_points(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    svc.handle_sample(_s(1))
    svc.handle_sample(_s(2))
    points = {p.measurement: p for p in svc.influx_points(T0 + timedelta(seconds=2))}
    assert points["fuel"].tags == {"method": "fuel_rate"}
    assert points["fuel"].fields["tank_gal"] == pytest.approx(47.0 - 3.6 / 3600, abs=1e-6)
    assert points["vehicle"].fields["speed_mph"] == 60.0
    assert points["fuel"].fields["stale"] is False
    assert points["fuel"].fields["calibrating"] is True  # 5E not yet VERIFIED (Decision #32)


def test_temperatures_become_an_engine_point(tmp_path: Path) -> None:
    from dataclasses import replace

    svc = _svc(tmp_path)
    svc.handle_sample(replace(_s(1), coolant_temp_c=80.0, oil_temp_c=85.0))
    points = {p.measurement: p for p in svc.influx_points(T0)}
    assert points["engine"].fields == {"coolant_temp_c": 80.0, "oil_temp_c": 85.0}
    svc.handle_sample(_s(2))  # a fast round carries no temperatures
    assert "engine" not in {p.measurement for p in svc.influx_points(T0)}


def test_no_tank_means_no_fuel_point(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_sample(_s(1))
    assert [p.measurement for p in svc.influx_points(T0)] == ["vehicle"]


def test_manual_full_command_from_the_ha_button(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.handle_command("bigair/fill_up/manual", b"full", T0)
    assert svc.model is not None and svc.model.tank_gal == 47.0


@pytest.mark.parametrize(
    ("topic", "payload"), [("bigair/fill_up/manual", b"half"), ("other/topic", b"full")]
)
def test_unknown_commands_are_ignored(tmp_path: Path, topic: str, payload: bytes) -> None:
    svc = _svc(tmp_path)
    assert not svc.handle_command(topic, payload, T0)
    assert svc.model is None
