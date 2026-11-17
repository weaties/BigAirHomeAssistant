"""Fill-detection acceptance test against the synthetic drive (xfail until M1)."""

from __future__ import annotations

import pytest

from bigair.fill_detect import FillDetector, FillDetectorConfig
from tests.conftest import Drive, parse_ts


@pytest.mark.xfail(
    strict=True, raises=NotImplementedError, reason="M1: fill detection not built yet"
)
def test_exactly_one_candidate_after_ignition_on(drive: Drive) -> None:
    exp = drive.expected["fill_up"]
    detector = FillDetector(FillDetectorConfig(capacity_gal=drive.expected["capacity_gal"]))
    candidates = [
        c
        for sample, fix in zip(drive.obd, drive.gps, strict=True)
        if (c := detector.update(sample, fix)) is not None
    ]
    assert len(candidates) == exp["candidates_expected"]
    (candidate,) = candidates
    assert parse_ts(exp["candidate_after"]) <= candidate.ts <= parse_ts(exp["candidate_before"])
    assert candidate.est_gallons == pytest.approx(exp["gallons"], abs=0.5)
    assert candidate.lat is not None and candidate.lon is not None
