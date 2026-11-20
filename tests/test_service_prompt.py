"""Detected fills → prompt → reply through the service (#11)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.fill_prompt import CANDIDATE_TOPIC
from bigair.service import FILL_RESULT_TOPIC, FuelService, ServiceConfig
from bigair.signals import ObdSample

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
TOPIC = "bigair/fill_up/response"


def _s(
    t: int, mph: float | None, gph: float | None, level: float | None, ignition: bool = True
) -> ObdSample:
    return ObdSample(
        ts=T0 + timedelta(seconds=t),
        ignition=ignition,
        speed_mph=mph,
        fuel_rate_gph=gph,
        fuel_level_pct=level,
    )


def _fill_at_pump(svc: FuelService) -> tuple[str, int]:
    """Drive 60 mi at 15 mpg, park, fill (level 40 → 90 %), restart. Returns (fill_id, t)."""
    t = 0
    for _ in range(3601):
        svc.handle_sample(_s(t, 60.0, 4.0, 40.0))
        t += 1
    for _ in range(60):
        svc.handle_sample(_s(t, 0.0, 0.6, 40.0))
        t += 1
    for _ in range(600):
        svc.handle_sample(_s(t, None, None, None, ignition=False))
        t += 1
    for _ in range(60):
        svc.handle_sample(_s(t, 0.0, 0.6, 90.0))
        t += 1
    prompts = [json.loads(p) for topic, p in svc.drain_outbox() if topic == CANDIDATE_TOPIC]
    assert len(prompts) == 1
    return str(prompts[0]["fill_id"]), t


def _svc(tmp_path: Path) -> FuelService:
    svc = FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))
    svc.handle_manual_full(T0 - timedelta(seconds=1))
    return svc


def test_detected_fill_is_prompted_and_flagged_pending(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    _fill_at_pump(svc)
    assert json.loads(svc.state_payload(T0 + timedelta(hours=2)))["fill_pending"] is True
    assert svc.gauge_feed(T0 + timedelta(hours=2))["fill_pending"] is True


def test_tank_is_not_changed_while_the_fill_is_pending(tmp_path: Path) -> None:
    """Decision #34: no provisional application of the uncalibrated level estimate."""
    svc = _svc(tmp_path)
    _fill_at_pump(svc)
    assert svc.model is not None
    assert svc.model.tank_gal == pytest.approx(47.0 - 4.0 - 0.6 * 120 / 3600, abs=0.02)


def test_reply_applies_a_live_fill_as_of_detection(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    fill_id, t = _fill_at_pump(svc)
    for _ in range(600):  # drive 10 min before answering
        svc.handle_sample(_s(t, 60.0, 4.0, 90.0))
        t += 1
    svc.handle_command(
        TOPIC,
        json.dumps({"fill_id": fill_id, "kind": "full", "text": "4.1 4.19"}).encode(),
        T0 + timedelta(seconds=t),
    )
    assert svc.model is not None
    # Full as of detection, minus the 10 min (~0.67 gal) driven since.
    assert svc.model.tank_gal == pytest.approx(47.0 - 4.0 * 600 / 3600, abs=0.05)
    (p,) = svc.drain_points()
    assert p.tags["source"] == "live"
    assert p.ts < T0 + timedelta(seconds=t - 500)  # timestamp of detection, not the reply
    assert json.loads(svc.state_payload(T0 + timedelta(seconds=t)))["fill_pending"] is False


def test_not_a_fill_is_recorded_as_skipped(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    fill_id, t = _fill_at_pump(svc)
    svc.handle_command(
        TOPIC, json.dumps({"fill_id": fill_id, "kind": "skip"}).encode(), T0 + timedelta(seconds=t)
    )
    (p,) = svc.drain_points()
    assert p.tags["status"] == "skipped"
    assert p.fields["fill_id"] == fill_id
    results = [json.loads(m) for topic, m in svc.drain_outbox() if topic == FILL_RESULT_TOPIC]
    assert results[-1]["ok"] is True and "not a fill" in str(results[-1]["message"]).lower()


def test_later_keeps_it_pending(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    fill_id, t = _fill_at_pump(svc)
    svc.handle_command(
        TOPIC, json.dumps({"fill_id": fill_id, "kind": "later"}).encode(), T0 + timedelta(seconds=t)
    )
    assert json.loads(svc.state_payload(T0 + timedelta(seconds=t)))["fill_pending"] is True
