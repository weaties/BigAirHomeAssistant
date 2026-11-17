"""Tank level, gallons used, range and mpg — Critical tier.

Design: .claude/skills/fuel-model/SKILL.md. M0 defines the interface only;
the acceptance tests in tests/test_fuel_model_acceptance.py are
xfail(strict=True) until M1 implements it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bigair.signals import FillUp, FuelState, ObdSample

LITRES_PER_GAL = 3.785411784
ULSD_DENSITY_G_PER_L = 840.0  # assumption; corrected by pump-receipt calibration
DIESEL_AFR_STOICH = 14.5  # assumption; corrected by pump-receipt calibration


@dataclass(frozen=True)
class FuelModelConfig:
    capacity_gal: float = 47.0  # nominal; usable capacity UNVERIFIED
    rolling_window_mi: float = 150.0
    rolling_min_mi: float = 50.0
    prior_mpg: float = 15.0
    max_gap_s: float = 5.0
    instant_window_s: float = 30.0
    instant_min_mi: float = 0.05
    instant_cap_mpg: float = 50.0


class FuelModel:
    """Integrates engine fuel into a persisted tank state."""

    def __init__(self, config: FuelModelConfig, state: FuelState) -> None:
        self.config = config
        self.state = state

    def update(self, sample: ObdSample) -> FuelState:
        """Integrate one sample; return the new state."""
        raise NotImplementedError("M1: fuel integrator")

    def apply_fill(self, fill: FillUp) -> FuelState:
        """Reset counters on a confirmed fill (full → capacity, partial → +gallons)."""
        raise NotImplementedError("M1: fill reset")

    @property
    def tank_gal(self) -> float:
        raise NotImplementedError("M1: tank level")

    @property
    def mpg_rolling(self) -> float:
        raise NotImplementedError("M1: rolling mpg")

    @property
    def range_mi(self) -> float:
        raise NotImplementedError("M1: range")

    @property
    def mpg_instant_30s(self) -> float | None:
        raise NotImplementedError("M1: instant mpg")

    @property
    def mpg_since_full(self) -> float | None:
        """True mpg of the most recent full-to-full interval, if any."""
        raise NotImplementedError("M1: fill-to-fill mpg")
