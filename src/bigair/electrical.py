"""House electrical state from the Victron devices (#109).

Keeps the latest decoded reading per device. It gives the MQTT state that HA reads
(`ELECTRICAL_STATE_TOPIC`, retained) and throttled `electrical` points for
InfluxDB. Freshness and throttling use the edge's monotonic time, so a wall-clock
step can't make a reading look fresh or stale (#107). A device that has gone
quiet reads as unknown, never as its last value.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import TYPE_CHECKING, ClassVar

from bigair.influx_io import Point

if TYPE_CHECKING:
    from bigair.signals import VictronSample

INFLUX_EVERY_S = 10.0  # Victron advertises about once a second; 10 s is plenty of history
STALE_AFTER_S = 120.0

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
    KEYS: ClassVar[tuple[str, ...]] = tuple(_MAP)

    def __init__(self) -> None:
        self.latest: dict[str, VictronSample] = {}
        self._written: dict[str, float] = {}

    def update(self, s: VictronSample) -> Point | None:
        """Record a reading; return an Influx point when this device is due one."""
        self.latest[s.device] = s
        last = self._written.get(s.device)
        if last is not None and s.mono_s - last < INFLUX_EVERY_S:
            return None
        self._written[s.device] = s.mono_s
        values = {f.name: getattr(s, f.name) for f in fields(s) if f.name not in _NOT_FIELDS}
        return Point(
            "electrical",
            s.ts,
            tags={"device": s.device},
            fields={k: v for k, v in values.items() if v is not None},
        )

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
