"""Fill-detection state machine cases (#10). Rules: .claude/skills/fill-up-events/SKILL.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.fill_detect import DetectorState, FillDetector, FillDetectorConfig
from bigair.signals import FillCandidate, GpsFix, ObdSample

T0 = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)


class Van:
    """Feeds a detector one second at a time; level is percent of the sender's scale."""

    def __init__(self, detector: FillDetector | None = None) -> None:
        self.d = detector or FillDetector(FillDetectorConfig())
        self.t = 0
        self.candidates: list[FillCandidate] = []

    def run(
        self,
        seconds: int,
        level: float | None,
        mph: float = 0.0,
        ignition: bool = True,
        lat: float = 39.5,
    ) -> Van:
        for _ in range(seconds):
            ts = T0 + timedelta(seconds=self.t)
            sample = ObdSample(
                ts=ts,
                ignition=ignition,
                speed_mph=mph if ignition else None,
                fuel_rate_gph=1.0 if ignition else None,
                odometer_mi=1000.0 if ignition else None,
                fuel_level_pct=level if ignition else None,
            )
            c = self.d.update(sample, GpsFix(ts=ts, lat=lat, lon=-111.0))
            if c is not None:
                self.candidates.append(c)
            self.t += 1
        return self

    def drive(self, seconds: int = 300, level: float | None = 50.0) -> Van:
        return self.run(seconds, level, mph=40.0)

    def off(self, seconds: int = 600, lat: float = 39.5) -> Van:
        return self.run(seconds, None, ignition=False, lat=lat)


def test_fill_while_parked_gives_one_candidate() -> None:
    van = Van().drive(level=50.0).run(30, 50.0).off().run(60, 80.0).drive(level=80.0)
    (c,) = van.candidates
    assert c.est_gallons == pytest.approx(0.30 * 47.0)
    assert c.odometer_mi == 1000.0
    assert c.fill_id


def test_candidate_comes_after_the_settle_time() -> None:
    van = Van().drive(level=50.0).off().run(44, 80.0)
    assert van.candidates == []
    van.run(2, 80.0)
    assert len(van.candidates) == 1


def test_candidate_carries_the_last_fix_while_parked() -> None:
    van = Van().drive(level=50.0).off(lat=40.25).run(60, 80.0, lat=40.30)
    (c,) = van.candidates
    assert c.lat == pytest.approx(40.25)


def test_small_rise_is_not_a_fill() -> None:
    # 4 % of 47 gal = 1.88 gal, below max(2.0 gal, 5 % of capacity = 2.35 gal)
    van = Van().drive(level=50.0).off().run(60, 54.0).drive(level=54.0)
    assert van.candidates == []


def test_slosh_spike_while_driving_is_not_a_fill() -> None:
    van = Van().drive(level=50.0).drive(20, level=90.0).drive(level=50.0)
    assert van.candidates == []


def test_heater_overnight_level_drop_is_not_a_fill() -> None:
    van = Van().drive(level=50.0).off(8 * 3600).run(60, 46.0).drive(level=46.0)
    assert van.candidates == []


def test_ignition_cycled_twice_at_the_pump_gives_one_candidate() -> None:
    van = Van().drive(level=50.0).off().run(10, 80.0).off(120).run(60, 80.0)
    van.off(120).run(60, 80.0).drive(level=80.0)
    assert len(van.candidates) == 1


def test_moving_off_before_settling_gives_no_candidate_and_does_not_stick() -> None:
    van = Van().drive(level=50.0).off().run(20, 80.0).drive(level=80.0)
    assert van.candidates == []
    assert van.d.state is DetectorState.DRIVING
    # A later genuine fill is still caught.
    van.off().run(60, 95.0)
    assert len(van.candidates) == 1


def test_stopped_with_engine_running_counts_as_parked() -> None:
    """Speed 0 for 60 s parks even without ignition off; settling starts at ignition on."""
    van = Van().drive(level=50.0).run(90, 50.0).off(300).run(60, 80.0)
    assert len(van.candidates) == 1


def test_baseline_ignores_slosh_with_a_median() -> None:
    van = Van().drive(level=50.0)
    van.run(25, 50.0).run(5, 99.0)  # brief spike in the last 30 s before the stop
    van.off().run(60, 50.0)
    assert van.candidates == []


def test_no_level_signal_disables_detection_cleanly() -> None:
    van = Van().drive(level=None).off().run(60, None).drive(level=None)
    assert van.candidates == []
    assert van.d.state is DetectorState.DRIVING


def test_sender_calibration_table_maps_level_to_gallons() -> None:
    """The stock sender is non-linear on this tank: a learned table maps it (Decision #31)."""
    cfg = FillDetectorConfig(level_table=((0.0, 0.0), (50.0, 0.25), (100.0, 1.0)))
    van = Van(FillDetector(cfg)).drive(level=50.0).off().run(60, 100.0)
    (c,) = van.candidates
    assert c.est_gallons == pytest.approx(0.75 * 47.0)


def test_candidate_ids_are_unique() -> None:
    van = Van().drive(level=20.0).off().run(60, 50.0).drive(level=50.0)
    van.off().run(60, 90.0)
    a, b = van.candidates
    assert a.fill_id != b.fill_id
