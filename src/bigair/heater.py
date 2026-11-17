"""Webasto Air Top fuel accounting — Critical tier.

Design: .claude/skills/heater-accounting/SKILL.md. Variant, burn rates and
pump stroke volume are UNVERIFIED; no numbers live here until the heater's
label and datasheet are recorded in docs/pid-register.md.
"""

from __future__ import annotations


def gph_from_pump(pump_hz: float, ml_per_stroke: float) -> float:
    """Heater fuel rate from dosing-pump frequency."""
    raise NotImplementedError("M2: heater pulses")


def gph_from_runtime(power_level: int, burn_rate_gph: dict[int, float]) -> float:
    """Heater fuel rate from power level and the variant's datasheet table."""
    raise NotImplementedError("M2: heater runtime")
