"""Daily energy by source and by load, from the Victron readings (#113).

Integrated in bigair rather than Grafana, on the edge's monotonic clock (#107), and
never across a gap over `MAX_GAP_S`. Grafana's integral() would interpolate straight
across an overnight power-off.

Flows, all in W, at each battery-monitor reading:
- **net**: the BMV's power, positive while charging the house battery.
- **solar**: the MPPT's battery-side power (V × A), if it was heard recently.
- **loads**: solar − net while the alternator charger is off. Shore power isn't
  wired, so those are the only sources. Shore would show up here as negative loads.
- While the Orion charges, its Instant Readout has no current, so loads are **held**
  at their last value and **alternator** = net − solar + loads. Both are estimates
  (`estimated`) until the INA228 load node measures loads directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, tzinfo
from typing import TYPE_CHECKING, Any

from bigair.influx_io import Point

if TYPE_CHECKING:
    from bigair.signals import VictronSample

MAX_GAP_S = 120.0  # BLE bursts are 30 s apart; anything longer is an outage
FRESH_S = 120.0  # how long an MPPT or Orion reading stays usable
CHARGING_STATES = frozenset(
    {"bulk", "absorption", "float", "storage", "equalize", "repeated_absorption", "power_supply"}
)


@dataclass(frozen=True)
class Flows:
    net_w: float
    solar_w: float | None
    loads_w: float | None
    alternator_w: float | None
    estimated: bool = False


@dataclass
class DayTotals:
    day: str  # local date, ISO
    solar_wh: float = 0.0
    alternator_wh: float = 0.0
    battery_in_wh: float = 0.0
    battery_out_wh: float = 0.0
    loads_wh: float = 0.0
    estimated_wh: float = 0.0  # the part of loads_wh that was held, not measured


@dataclass
class _Last:
    mono_s: float
    flows: Flows


class EnergyMeter:
    def __init__(self, tz: tzinfo, saved: dict[str, Any] | None = None) -> None:
        self.tz = tz
        self.today: DayTotals = DayTotals(day="")
        self.yesterday: DayTotals | None = None
        self.flows: Flows = Flows(0.0, None, None, None)
        self._solar: VictronSample | None = None
        self._dcdc: VictronSample | None = None
        self._held_loads: float | None = None
        self._last: _Last | None = None
        self._last_ts: datetime | None = None
        if saved and isinstance(saved.get("today"), dict):
            self.today = DayTotals(**saved["today"])

    def update(self, s: VictronSample) -> None:
        if s.device == "mppt":
            self._solar = s
            return
        if s.device == "dcdc":
            self._dcdc = s
            return
        if s.device != "bmv" or s.power_w is None:
            return
        flows = self._flows(s)
        self._roll(s.ts.astimezone(self.tz).date())
        last = self._last
        if last is not None and 0 < (dt := s.mono_s - last.mono_s) <= MAX_GAP_S:
            self._integrate(last.flows, flows, dt / 3600)
        self._last = _Last(s.mono_s, flows)
        self._last_ts = s.ts
        self.flows = flows

    def _fresh(self, other: VictronSample | None, now_mono: float) -> VictronSample | None:
        return other if other is not None and now_mono - other.mono_s <= FRESH_S else None

    def _flows(self, s: VictronSample) -> Flows:
        net = float(s.power_w or 0.0)
        mppt = self._fresh(self._solar, s.mono_s)
        solar = None
        if mppt is not None and mppt.voltage_v is not None and mppt.current_a is not None:
            solar = max(mppt.voltage_v * mppt.current_a, 0.0)
        dcdc = self._fresh(self._dcdc, s.mono_s)
        charging = dcdc is not None and dcdc.state in CHARGING_STATES
        if not charging:
            loads = None if solar is None else max(solar - net, 0.0)
            if loads is not None:
                self._held_loads = loads
            return Flows(net, solar, loads, 0.0)
        loads = self._held_loads
        alt = None if solar is None or loads is None else max(net - solar + loads, 0.0)
        return Flows(net, solar, loads, alt, estimated=True)

    def _integrate(self, a: Flows, b: Flows, hours: float) -> None:
        def avg(x: float | None, y: float | None) -> float | None:
            return None if x is None or y is None else (x + y) / 2 * hours

        t = self.today
        t.battery_in_wh += (max(a.net_w, 0.0) + max(b.net_w, 0.0)) / 2 * hours
        t.battery_out_wh += (max(-a.net_w, 0.0) + max(-b.net_w, 0.0)) / 2 * hours
        if (wh := avg(a.solar_w, b.solar_w)) is not None:
            t.solar_wh += wh
        if (wh := avg(a.alternator_w, b.alternator_w)) is not None:
            t.alternator_wh += wh
        if (wh := avg(a.loads_w, b.loads_w)) is not None:
            t.loads_wh += wh
            if a.estimated or b.estimated:
                t.estimated_wh += wh

    def _roll(self, local_day: date) -> None:
        day = local_day.isoformat()
        if not self.today.day:
            self.today = DayTotals(day=day)
        elif day > self.today.day:  # only forward: a pre-sync clock can be days behind
            self.yesterday = self.today
            self.today = DayTotals(day=day)

    def to_state(self) -> dict[str, Any]:
        return {"today": asdict(self.today)}

    def day_point(self, totals: DayTotals | None = None) -> Point:
        """Running totals for a day, stamped at its local midnight, so rewriting it
        overwrites the same Influx point: one point per day."""
        t = totals or self.today
        start = datetime.combine(date.fromisoformat(t.day), time(0), self.tz).astimezone(UTC)
        fields: dict[str, float | int | bool | str | None] = {
            k: round(v, 2) for k, v in asdict(t).items() if k != "day"
        }
        return Point("energy", start, fields=fields)

    def payload(self) -> dict[str, Any]:
        """Keys for the HA state payload (merged into bigair/electrical/state)."""
        t = self.today
        f = self.flows
        return {
            "loads_w": None if f.loads_w is None else round(f.loads_w, 1),
            "alternator_w": None if f.alternator_w is None else round(f.alternator_w, 1),
            "energy_estimated": f.estimated,
            "solar_today_wh": round(t.solar_wh, 1) if t.day else None,
            "alternator_today_wh": round(t.alternator_wh, 1) if t.day else None,
            "loads_today_wh": round(t.loads_wh, 1) if t.day else None,
            "battery_in_today_wh": round(t.battery_in_wh, 1) if t.day else None,
            "battery_out_today_wh": round(t.battery_out_wh, 1) if t.day else None,
        }
