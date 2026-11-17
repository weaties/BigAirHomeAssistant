"""Fill-up detection state machine — Critical tier.

Design: .claude/skills/fill-up-events/SKILL.md. Interface only in M0.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bigair.signals import FillCandidate, GpsFix, ObdSample


class DetectorState(StrEnum):
    DRIVING = "driving"
    PARKED = "parked"
    SETTLING = "settling"


@dataclass(frozen=True)
class FillDetectorConfig:
    capacity_gal: float = 47.0
    min_jump_gal: float = 2.0
    min_jump_fraction: float = 0.05
    park_after_s: float = 60.0
    settle_s: float = 45.0
    baseline_window_s: float = 30.0


class FillDetector:
    def __init__(self, config: FillDetectorConfig) -> None:
        self.config = config
        self.state = DetectorState.DRIVING

    def update(self, sample: ObdSample, fix: GpsFix | None = None) -> FillCandidate | None:
        """Feed one sample; return a candidate exactly once per detected fill."""
        raise NotImplementedError("M1 #10: fill detection")
