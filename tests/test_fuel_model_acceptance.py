"""Fuel-model acceptance tests against the synthetic drive.

Built in #7 (integrator), #8 (mpg, range) and #9 (fills).
"""

from __future__ import annotations

import pytest

from bigair.fuel_model import FuelModel, FuelModelConfig
from bigair.signals import FillSource, FillStatus, FillType, FillUp, FuelState
from tests.support import Drive, parse_ts


def _fresh_model(drive: Drive) -> FuelModel:
    exp = drive.expected
    state = FuelState(
        ts=parse_ts(exp["times"]["start"]),
        gal_after_last_fill=exp["initial"]["tank_gal"],
        engine_used_gal=0.0,
        heater_used_gal=0.0,
        last_fill_type=FillType.FULL,
    )
    return FuelModel(FuelModelConfig(prior_mpg=exp["initial"]["prior_mpg"]), state)


def _run(model: FuelModel, drive: Drive, until: str) -> None:
    for sample in drive.until(parse_ts(until)):
        if sample.ts >= model.state.ts:
            model.update(sample)


def _fill(drive: Drive) -> FillUp:
    exp = drive.expected
    return FillUp(
        fill_id="fixture-fill",
        ts=parse_ts(exp["fill_up"]["candidate_after"]),
        source=FillSource.LIVE,
        fill_type=FillType.FULL,
        status=FillStatus.CONFIRMED,
        gallons=exp["fill_up"]["gallons"],
    )


def test_tank_range_and_mpg_before_fill(drive: Drive) -> None:
    exp = drive.expected["pre_fill"]
    model = _fresh_model(drive)
    _run(model, drive, exp["at"])
    assert model.tank_gal == pytest.approx(exp["tank_gal"], abs=0.01)
    assert model.mpg_rolling == pytest.approx(exp["mpg_rolling"], abs=0.01)
    assert model.range_mi == pytest.approx(exp["range_mi"], abs=1.0)


def test_full_fill_resets_tank_and_gives_true_mpg(drive: Drive) -> None:
    model = _fresh_model(drive)
    _run(model, drive, drive.expected["pre_fill"]["at"])
    model.apply_fill(_fill(drive))
    assert model.tank_gal == pytest.approx(drive.expected["fill_up"]["tank_after_gal"], abs=0.01)
    assert model.mpg_since_full == pytest.approx(
        drive.expected["fill_up"]["mpg_since_full"], abs=0.01
    )


def test_tank_range_and_mpg_at_end(drive: Drive) -> None:
    exp = drive.expected
    model = _fresh_model(drive)
    _run(model, drive, exp["times"]["ignition_on"])
    model.apply_fill(_fill(drive))
    _run(model, drive, "2026-06-01T23:59:59Z")
    assert model.tank_gal == pytest.approx(exp["end"]["tank_gal"], abs=0.01)
    assert model.mpg_rolling == pytest.approx(exp["end"]["mpg_rolling"], abs=0.01)
    assert model.range_mi == pytest.approx(exp["end"]["range_mi"], abs=1.0)


def test_instant_mpg_on_highway(drive: Drive) -> None:
    model = _fresh_model(drive)
    _run(model, drive, drive.expected["times"]["highway_mid"])
    assert model.mpg_instant_30s == pytest.approx(
        drive.expected["instant_mpg_30s"]["highway_mid"], abs=0.01
    )


def test_instant_mpg_is_none_at_idle(drive: Drive) -> None:
    model = _fresh_model(drive)
    _run(model, drive, drive.expected["times"]["idle_mid"])
    assert model.mpg_instant_30s is None
