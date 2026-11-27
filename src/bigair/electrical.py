"""House electrical state from the Victron devices (#109).

Keeps the latest decoded reading per device. It gives the MQTT state that HA reads
(`ELECTRICAL_STATE_TOPIC`, retained) and throttled `electrical` points for
InfluxDB. Freshness and throttling use the edge's monotonic time, so a wall-clock
step can't make a reading look fresh or stale (#107). A device that has gone
quiet reads as unknown, never as its last value.

With an EnergyMeter (#113) it also writes the derived house flows (`electrical`,
device=house: loads_w, alternator_w) with each battery-monitor point, plus today's
running energy totals (`energy`) every minute and once more when the day rolls over.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import TYPE_CHECKING, ClassVar

from bigair.influx_io import Point

if TYPE_CHECKING:
    from bigair.energy import EnergyMeter
    from bigair.signals import VictronSample

INFLUX_EVERY_S = 10.0  # Victron advertises about once a second; 10 s is plenty of history
STALE_AFTER_S = 120.0
ENERGY_EVERY_S = 60.0  # rewrites the same daily point, so cheap

_NOT_FIELDS = {"ts", "mono_s", "device"}


class ElectricalTracker:
    # Payload key → (device, VictronSample attribute). The keys are HA's value templates.
    _MAP: ClassVar[dict[str, tuple[str, str]]] = {
        "house_soc_pct": ("bmv", "soc_pct"),
        "house_voltage_v": ("bmv", "voltage_v"),
        "house_current_a": ("bmv", "current_a"),
        "house_power_w": ("bmv", "power_w"),
        "house_consumed_ah": ("bmv", "consumed_ah"),
        "house_time_to_go_min": ("bmv", "time_to_go_s"),
        "solar_power_w": ("mppt", "pv_power_w"),
        "solar_yield_today_wh": ("mppt", "yield_today_wh"),
        "solar_current_a": ("mppt", "current_a"),
        "solar_state": ("mppt", "state"),
        "dcdc_state": ("dcdc", "state"),
        "dcdc_input_voltage_v": ("dcdc", "input_voltage_v"),
        "dcdc_output_voltage_v": ("dcdc", "voltage_v"),
    }
    ENERGY_KEYS: ClassVar[tuple[str, ...]] = (
        "loads_w",
        "alternator_w",
        "energy_estimated",
        "solar_today_wh",
        "alternator_today_wh",
        "loads_today_wh",
        "battery_in_today_wh",
        "battery_out_today_wh",
    )
    KEYS: ClassVar[tuple[str, ...]] = (*_MAP, *ENERGY_KEYS)

    def __init__(self, meter: EnergyMeter | None = None) -> None:
        self.latest: dict[str, VictronSample] = {}
        self.meter = meter
        self._written: dict[str, float] = {}
        self._energy_written: float | None = None

    def update(self, s: VictronSample) -> list[Point]:
        """Record a reading; return the Influx points that are due."""
        self.latest[s.device] = s
        points: list[Point] = []
        if self.meter is not None:
            before = self.meter.today.day
            self.meter.update(s)
            if before and self.meter.yesterday and self.meter.yesterday.day == before:
                points.append(self.meter.day_point(self.meter.yesterday))  # final totals
                self._energy_written = None
        last = self._written.get(s.device)
        if last is not None and s.mono_s - last < INFLUX_EVERY_S:
            return points
        self._written[s.device] = s.mono_s
        values = {f.name: getattr(s, f.name) for f in fields(s) if f.name not in _NOT_FIELDS}
        points.append(
            Point(
                "electrical",
                s.ts,
                tags={"device": s.device},
                fields={k: v for k, v in values.items() if v is not None},
            )
        )
        if self.meter is not None and s.device == "bmv":
            f = self.meter.flows
            house = {"loads_w": f.loads_w, "alternator_w": f.alternator_w}
            if any(v is not None for v in house.values()):
                fields_ = {k: round(v, 1) for k, v in house.items() if v is not None}
                points.append(
                    Point(
                        "electrical",
                        s.ts,
                        tags={"device": "house"},
                        fields={**fields_, "estimated": f.estimated},
                    )
                )
            due = self._energy_written is None or s.mono_s - self._energy_written >= ENERGY_EVERY_S
            if due and self.meter.today.day:
                self._energy_written = s.mono_s
                points.append(self.meter.day_point())
        return points

    def state_payload(self, mono_now: float) -> str:
        out: dict[str, object] = {}
        for key, (device, attr) in self._MAP.items():
            s = self.latest.get(device)
            value = None
            if s is not None and mono_now - s.mono_s <= STALE_AFTER_S:
                value = getattr(s, attr)
            if key == "house_time_to_go_min" and value is not None:
                value = int(value) // 60
            if key == "house_power_w" and value is not None:
                value = round(float(value), 1)
            out[key] = value
        energy: dict[str, object] = dict.fromkeys(self.ENERGY_KEYS)
        if self.meter is not None:
            energy.update(self.meter.payload())
            bmv = self.latest.get("bmv")
            if bmv is None or mono_now - bmv.mono_s > STALE_AFTER_S:
                energy.update(loads_w=None, alternator_w=None)  # live flows go unknown
        out.update(energy)
        return json.dumps(out, sort_keys=True)


def victron_keys(env: dict[str, str]) -> tuple[dict[str, bytes], list[str]]:
    """{MAC: key} from VICTRON_<NAME>_MAC / VICTRON_<NAME>_KEY pairs, plus problems.

    A pair that's incomplete or malformed is skipped and reported, never guessed at.
    """
    keys: dict[str, bytes] = {}
    problems: list[str] = []
    names = sorted(
        k[len("VICTRON_") : -len("_MAC")]
        for k in env
        if k.startswith("VICTRON_") and k.endswith("_MAC")
    )
    for name in names:
        mac = env.get(f"VICTRON_{name}_MAC", "").strip().upper()
        key = env.get(f"VICTRON_{name}_KEY", "").strip()
        if len(mac.split(":")) != 6 or not all(
            len(p) == 2 and all(c in "0123456789ABCDEF" for c in p) for p in mac.split(":")
        ):
            problems.append(f"VICTRON_{name}_MAC isn't a MAC address")
            continue
        try:
            raw = bytes.fromhex(key)
        except ValueError:
            raw = b""
        if len(raw) != 16:
            problems.append(f"VICTRON_{name}_KEY isn't 32 hex digits")
            continue
        keys[mac] = raw
    return keys, problems
