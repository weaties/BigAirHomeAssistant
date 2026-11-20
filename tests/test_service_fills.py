"""Logging fill-ups through the service (#12): apply, confirm, record, dedupe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.service import FILL_RESULT_TOPIC, FuelService, ServiceConfig
from bigair.signals import ObdSample

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
TOPIC = "bigair/fill_up/response"


def _svc(tmp_path: Path) -> FuelService:
    return FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))


def _resp(
    kind: str = "full", gallons: float = 4.0, price: float = 4.19, fill_id: str = "f1"
) -> bytes:
    return json.dumps(
        {"fill_id": fill_id, "kind": kind, "gallons": gallons, "price_per_gal": price}
    ).encode()


def _drive(svc: FuelService, start: int, seconds: int, mpg: float = 15.0) -> int:
    for i in range(seconds + 1):
        svc.handle_sample(
            ObdSample(
                ts=T0 + timedelta(seconds=start + i),
                ignition=True,
                speed_mph=60.0,
                fuel_rate_gph=60.0 / mpg,
            )
        )
    return start + seconds


def _results(svc: FuelService) -> list[dict[str, object]]:
    return [json.loads(p) for t, p in svc.drain_outbox() if t == FILL_RESULT_TOPIC]


def test_full_fill_with_gallons_gives_true_mpg(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    t = _drive(svc, 1, 3600)  # 60 mi on 4 gal
    svc.handle_command(TOPIC, _resp(gallons=4.0), T0 + timedelta(seconds=t + 60))
    assert svc.model is not None
    assert svc.model.tank_gal == pytest.approx(47.0)
    assert svc.model.mpg_since_full == pytest.approx(15.0, rel=0.01)
    (r,) = _results(svc)
    assert r["ok"] is True
    assert "full 4.0 gal" in str(r["message"])
    assert "15.0 mpg" in str(r["message"])


def test_partial_fill_adds_gallons(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    t = _drive(svc, 1, 3600)  # 4 gal used → 43
    svc.handle_command(TOPIC, _resp(kind="partial", gallons=2.5), T0 + timedelta(seconds=t + 9))
    assert svc.model is not None
    assert svc.model.tank_gal == pytest.approx(45.5, abs=0.02)


def test_fill_is_saved_immediately(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    svc.handle_command(TOPIC, _resp(kind="partial", gallons=1.0, fill_id="p1"), T0)
    saved = json.loads((tmp_path / "s.json").read_text())
    assert saved["state"]["last_fill_id"] == "p1"


def test_double_tap_with_the_same_id_is_applied_once(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    _drive(svc, 1, 3600)
    svc.handle_command(TOPIC, _resp(kind="partial", gallons=2.0), T0 + timedelta(hours=2))
    svc.handle_command(TOPIC, _resp(kind="partial", gallons=2.0), T0 + timedelta(hours=2))
    assert svc.model is not None
    assert svc.model.tank_gal == pytest.approx(45.0, abs=0.02)


def test_same_fill_resent_with_a_new_id_is_ignored(tmp_path: Path) -> None:
    """Double-tapping the script makes a new fill_id; same numbers within 10 min = duplicate."""
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    _drive(svc, 1, 3600)
    svc.handle_command(
        TOPIC, _resp(kind="partial", gallons=2.0, fill_id="a"), T0 + timedelta(hours=2)
    )
    svc.drain_outbox()
    svc.handle_command(
        TOPIC, _resp(kind="partial", gallons=2.0, fill_id="b"), T0 + timedelta(hours=2, minutes=1)
    )
    assert svc.model is not None
    assert svc.model.tank_gal == pytest.approx(45.0, abs=0.02)
    (r,) = _results(svc)
    assert r["ok"] is False and "duplicate" in str(r["message"])


def test_invalid_input_is_reported_not_applied(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    svc.handle_command(TOPIC, _resp(gallons=75.0), T0)
    (r,) = _results(svc)
    assert r["ok"] is False and "gallons" in str(r["message"])
    assert svc.model is not None and svc.model.state.last_fill_id is None


def test_full_fill_starts_the_tank_on_a_fresh_install(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_command(TOPIC, _resp(gallons=20.0), T0)
    assert svc.model is not None and svc.model.tank_gal == 47.0
    assert svc.model.mpg_since_full is None  # no interval to measure yet, not 0 mpg
    (r,) = _results(svc)
    assert r["ok"] is True and "started" in str(r["message"])


def test_partial_fill_needs_a_known_tank(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_command(TOPIC, _resp(kind="partial"), T0)
    assert svc.model is None
    (r,) = _results(svc)
    assert r["ok"] is False and "full" in str(r["message"])


def test_fill_is_recorded_for_influx(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.handle_manual_full(T0)
    _drive(svc, 1, 3600)
    svc.handle_command(
        TOPIC, _resp(gallons=4.0, price=4.19, fill_id="rec"), T0 + timedelta(hours=2)
    )
    (p,) = svc.drain_points()
    assert p.measurement == "fill_up"
    assert p.tags == {"source": "manual", "fill_type": "full", "status": "confirmed"}
    assert p.fields["gallons"] == 4.0
    assert p.fields["price_per_gal"] == 4.19
    assert p.fields["total_cost"] == pytest.approx(16.76)
    assert p.fields["tank_after_gal"] == 47.0
    assert p.fields["tank_before_gal"] == pytest.approx(43.0, abs=0.02)
    assert p.fields["fill_id"] == "rec"
