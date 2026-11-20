"""Track points from the service: mpg_30s, trip_id, fill-up location (#14)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.service import FuelService, ServiceConfig
from bigair.signals import GpsFix, ObdSample

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)


def _svc(tmp_path: Path) -> FuelService:
    svc = FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))
    svc.handle_manual_full(T0 - timedelta(seconds=1))
    return svc


def _fix(sec: float, mph: float = 60.0, lat: float = 39.5) -> GpsFix:
    return GpsFix(
        ts=T0 + timedelta(seconds=sec),
        lat=lat,
        lon=-111.0,
        speed_mph=mph,
        heading_deg=90.0,
        alt_m=1500.0,
    )


def test_track_point_carries_mpg_and_trip(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(60):
        svc.handle_sample(
            ObdSample(
                ts=T0 + timedelta(seconds=i), ignition=True, speed_mph=60.0, fuel_rate_gph=4.0
            )
        )
    p = svc.handle_fix(_fix(60))
    assert p is not None
    assert p.measurement == "track"
    assert p.tags == {"source": "gps_bt"}
    assert p.fields["mpg_30s"] == pytest.approx(15.0, rel=0.01)
    assert p.fields["lat"] == 39.5 and p.fields["speed_mph"] == 60.0
    assert isinstance(p.fields["trip_id"], str) and p.fields["trip_id"]


def test_fixes_are_thinned_to_the_track_cadence(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    kept = [svc.handle_fix(_fix(i * 0.1)) for i in range(100)]  # 10 s at 10 Hz
    assert sum(p is not None for p in kept) == 2


def test_no_trip_yet_leaves_trip_id_empty(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    p = svc.handle_fix(_fix(0, mph=0.0))
    assert p is not None and p.fields["trip_id"] is None


def test_fill_candidate_gets_the_parked_position(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    t = 0

    def run(
        n: int, mph: float | None, level: float | None, ign: bool = True, lat: float = 39.5
    ) -> None:
        nonlocal t
        for _ in range(n):
            svc.handle_fix(_fix(t, mph or 0.0, lat))
            svc.handle_sample(
                ObdSample(
                    ts=T0 + timedelta(seconds=t),
                    ignition=ign,
                    speed_mph=mph,
                    fuel_rate_gph=0.6 if ign else None,
                    fuel_level_pct=level,
                )
            )
            t += 1

    run(120, 0.0, 30.0)
    run(600, None, None, ign=False, lat=39.75)  # parked at the pump
    run(60, 0.0, 80.0, lat=39.76)
    prompts = [json.loads(p) for topic, p in svc.drain_outbox() if "candidate" in topic]
    assert len(prompts) == 1
    assert prompts[0]["lat"] == pytest.approx(39.75)
