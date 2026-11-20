"""Home Assistant MQTT discovery for bigair's derived sensors (#13).

Pure: builds (topic, payload) pairs; the MQTT edge publishes them retained.
Availability is bigair's LWT topic (unavailable when bigair dies). Tank and
range additionally go unavailable while the fuel state is stale — a wrong
fuel number is worse than no number (CLAUDE.md).

A null value in the state payload renders as "None", which HA's MQTT sensor
treats as unknown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "bigair"
AVAILABILITY_TOPIC = "bigair/status"  # LWT: bigair sets the will to OFFLINE, retained
FUEL_STATE_TOPIC = "bigair/fuel/state"  # retained JSON; display copy only (Decision #22)
MANUAL_FILL_TOPIC = "bigair/fill_up/manual"  # HA button → bigair ("full")
FILL_RESPONSE_TOPIC = "bigair/fill_up/response"  # HA form / notification reply → bigair
FILL_RESULT_TOPIC = "bigair/fill_up/result"  # bigair → HA: what was logged, or why not
ONLINE = "online"
OFFLINE = "offline"

DEVICE = {
    "identifiers": ["bigair"],
    "name": "BigAir van",
    "manufacturer": "BigAir",
    "model": "Sprinter telemetry",
}

_ONLINE = {
    "topic": AVAILABILITY_TOPIC,
    "payload_available": ONLINE,
    "payload_not_available": OFFLINE,
}
_NOT_STALE = {
    "topic": FUEL_STATE_TOPIC,
    "value_template": "{{ 'offline' if value_json.stale else 'online' }}",
    "payload_available": ONLINE,
    "payload_not_available": OFFLINE,
}


@dataclass(frozen=True)
class _Sensor:
    object_id: str
    name: str
    unit: str
    precision: int
    device_class: str | None = None
    icon: str | None = None
    stale_gated: bool = False


SENSORS = (
    _Sensor("tank_gal", "Fuel tank", "gal", 1, "volume_storage", stale_gated=True),
    _Sensor("range_mi", "Range", "mi", 0, "distance", stale_gated=True),
    _Sensor("mpg_rolling", "Fuel economy (150 mi)", "mpg", 1, icon="mdi:gas-station"),
    _Sensor("mpg_instant_30s", "Fuel economy (30 s)", "mpg", 1, icon="mdi:speedometer"),
    _Sensor("mpg_since_full", "Fuel economy (last fill)", "mpg", 1, icon="mdi:fuel"),
)

_ROUNDING = {
    "tank_gal": 2,
    "range_mi": 0,
    "mpg_rolling": 2,
    "mpg_instant_30s": 2,
    "mpg_since_full": 2,
}


def _sensor_config(s: _Sensor) -> dict[str, object]:
    config: dict[str, object] = {
        "name": s.name,
        "object_id": s.object_id,
        "unique_id": f"bigair_{s.object_id}",
        "state_topic": FUEL_STATE_TOPIC,
        "value_template": f"{{{{ value_json.{s.object_id} }}}}",
        "unit_of_measurement": s.unit,
        "state_class": "measurement",
        "suggested_display_precision": s.precision,
        "device": DEVICE,
        "availability": [_ONLINE, _NOT_STALE] if s.stale_gated else [_ONLINE],
        "availability_mode": "all",
    }
    if s.device_class:
        config["device_class"] = s.device_class
    if s.icon:
        config["icon"] = s.icon
    return config


def _stale_config() -> dict[str, object]:
    return {
        "name": "Fuel data stale",
        "object_id": "fuel_stale",
        "unique_id": "bigair_fuel_stale",
        "state_topic": FUEL_STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.stale else 'OFF' }}",
        "device_class": "problem",
        "device": DEVICE,
        "availability": [_ONLINE],
        "availability_mode": "all",
    }


def _calibrating_config() -> dict[str, object]:
    return {
        "name": "Fuel rate calibrating",
        "object_id": "fuel_calibrating",
        "unique_id": "bigair_fuel_calibrating",
        "state_topic": FUEL_STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.calibrating else 'OFF' }}",
        "icon": "mdi:scale-balance",
        "device": DEVICE,
        "availability": [_ONLINE],
        "availability_mode": "all",
    }


def _fill_pending_config() -> dict[str, object]:
    return {
        "name": "Fill-up pending",
        "object_id": "fill_pending",
        "unique_id": "bigair_fill_pending",
        "state_topic": FUEL_STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.fill_pending else 'OFF' }}",
        "icon": "mdi:gas-station-outline",
        "device": DEVICE,
        "availability": [_ONLINE],
        "availability_mode": "all",
    }


def _last_fill_config() -> dict[str, object]:
    return {
        "name": "Last fill-up",
        "object_id": "last_fill_up",
        "unique_id": "bigair_last_fill_up",
        "state_topic": FILL_RESULT_TOPIC,
        "value_template": "{{ value_json.message[:250] }}",
        "json_attributes_topic": FILL_RESULT_TOPIC,
        "icon": "mdi:receipt-text",
        "device": DEVICE,
        "availability": [_ONLINE],
        "availability_mode": "all",
    }


def _filled_to_full_config() -> dict[str, object]:
    return {
        "name": "Filled to full",
        "object_id": "filled_to_full",
        "unique_id": "bigair_filled_to_full",
        "command_topic": MANUAL_FILL_TOPIC,
        "payload_press": "full",
        "icon": "mdi:gas-station",
        "device": DEVICE,
        "availability": [_ONLINE],
        "availability_mode": "all",
    }


def discovery_messages() -> list[tuple[str, str]]:
    """Retained discovery configs: (topic, JSON payload)."""
    out = [
        (
            f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{s.object_id}/config",
            json.dumps(_sensor_config(s), sort_keys=True),
        )
        for s in SENSORS
    ]
    out.append(
        (
            f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/fuel_stale/config",
            json.dumps(_stale_config(), sort_keys=True),
        )
    )
    out.append(
        (
            f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/fuel_calibrating/config",
            json.dumps(_calibrating_config(), sort_keys=True),
        )
    )
    out.append(
        (
            f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/fill_pending/config",
            json.dumps(_fill_pending_config(), sort_keys=True),
        )
    )
    out.append(
        (
            f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/last_fill_up/config",
            json.dumps(_last_fill_config(), sort_keys=True),
        )
    )
    out.append(
        (
            f"{DISCOVERY_PREFIX}/button/{NODE_ID}/filled_to_full/config",
            json.dumps(_filled_to_full_config(), sort_keys=True),
        )
    )
    return out


def fuel_state_payload(
    ts: datetime,
    tank_gal: float | None,
    range_mi: float | None,
    mpg_rolling: float | None,
    mpg_instant_30s: float | None,
    mpg_since_full: float | None,
    stale: bool,
    *,
    calibrating: bool = False,
    fill_pending: bool = False,
) -> str:
    """The retained JSON on FUEL_STATE_TOPIC (keys match the sensor object_ids)."""
    values = {
        "tank_gal": tank_gal,
        "range_mi": range_mi,
        "mpg_rolling": mpg_rolling,
        "mpg_instant_30s": mpg_instant_30s,
        "mpg_since_full": mpg_since_full,
    }
    payload: dict[str, object] = {
        k: None if v is None else round(float(v), _ROUNDING[k]) for k, v in values.items()
    }
    payload["ts"] = ts.isoformat()
    payload["stale"] = stale
    payload["calibrating"] = calibrating  # 5E not yet VERIFIED (Decision #32)
    payload["fill_pending"] = fill_pending  # a detected fill awaits Dan's answer (#11)
    return json.dumps(payload, sort_keys=True)
