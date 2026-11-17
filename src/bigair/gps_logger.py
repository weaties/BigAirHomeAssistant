"""USB GPS track logging via gpsd.

M1: an async gpsd JSON client (edge) turns TPV reports into GpsFix; this
module decides when to emit a track point (every few seconds while moving)
and attaches mpg_30s from the fuel model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bigair.signals import GpsFix


def fix_from_tpv(report: dict[str, Any]) -> GpsFix | None:
    """Convert a gpsd TPV report to a GpsFix (None without a 2D/3D fix)."""
    raise NotImplementedError("M1: gpsd TPV decode")
