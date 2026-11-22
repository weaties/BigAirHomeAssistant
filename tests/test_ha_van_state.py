"""Trip, battery, ignition and freshness entities for the HA phone dash (#64)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from bigair.ha_discovery import VAN_STATE_TOPIC, discovery_messages, van_state_payload
from bigair.service import FuelService, ServiceConfig
from bigair.signals import GpsFix, ObdSample

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)

VAN_IDS = {"trip_mi", "trip_gal", "trip_mpg", "trip_start", "van_battery_v", "obd_ts", "gps_ts"}


def _by_id() -> dict[str, dict[str, object]]:
    out = {}
    for _topic, payload in discovery_messages():
        c = json.loads(payload)
        out[str(c["object_id"])] = c
    return out


def test_van_entities_are_discovered_on_the_van_topic() -> None:
    by_id = _by_id()
    for oid in [*VAN_IDS, "ignition"]:
        assert by_id[oid]["state_topic"] == VAN_STATE_TOPIC, oid


def test_units_classes_and_names() -> None:
    by_id = _by_id()
    assert by_id["trip_mi"]["unit_of_measurement"] == "mi"
    assert by_id["trip_mi"]["device_class"] == "distance"
    assert by_id["trip_gal"]["unit_of_measurement"] == "gal"
    assert by_id["trip_mpg"]["unit_of_measurement"] == "mpg"
    assert by_id["van_battery_v"]["unit_of_measurement"] == "V"
    assert by_id["van_battery_v"]["device_class"] == "voltage"
    for oid in ("trip_start", "obd_ts", "gps_ts"):
        assert by_id[oid]["device_class"] == "timestamp", oid
        assert "unit_of_measurement" not in by_id[oid]
        assert "state_class" not in by_id[oid]
    # Names drive HA's entity ids (sensor.bigair_van_<name>); the dashboard relies on them.
    names = {oid: by_id[oid]["name"] for oid in [*VAN_IDS, "ignition"]}
    assert names == {
        "trip_mi": "Trip distance",
        "trip_gal": "Trip fuel",
        "trip_mpg": "Trip economy",
        "trip_start": "Trip started",
        "van_battery_v": "Battery",
        "obd_ts": "Engine last seen",
        "gps_ts": "GPS last fix",
        "ignition": "Ignition",
    }


def test_ignition_is_a_binary_sensor_with_unknown() -> None:
    topics = dict(discovery_messages())
    cfg = json.loads(topics["homeassistant/binary_sensor/bigair/ignition/config"])
    assert cfg["device_class"] == "power"
    tpl = str(cfg["value_template"])
    assert "none" in tpl and "ON" in tpl and "OFF" in tpl


def test_payload_keeps_only_the_van_keys_and_nulls() -> None:
    feed: dict[str, object] = {
        "ts": "2026-11-21T18:00:00Z",
        "tank_gal": 30.0,
        "trip_mi": 6.0,
        "trip_gal": 0.4,
        "trip_mpg": 15.0,
        "trip_start": "2026-11-21T17:50:00Z",
        "van_battery_v": None,
        "ignition": True,
        "obd_ts": "2026-11-21T18:00:00Z",
        "gps_ts": None,
        "gps_lat": 1.0,
    }
    payload = json.loads(van_state_payload(feed))
    assert set(payload) == {*VAN_IDS, "ignition"}
    assert payload["van_battery_v"] is None
    assert payload["gps_ts"] is None
    assert payload["trip_start"] == "2026-11-21T17:50:00Z"
    assert "gps_lat" not in payload  # position stays out of HA's history


def test_service_publishes_the_van_state(tmp_path: Path) -> None:
    svc = FuelService.start(ServiceConfig(state_path=tmp_path / "s.json"))
    for i in range(361):
        svc.handle_sample(
            ObdSample(
                ts=T0 + timedelta(seconds=i),
                ignition=True,
                speed_mph=60.0,
                fuel_rate_gph=4.0,
                battery_v=14.1,
            )
        )
    svc.handle_fix(GpsFix(ts=T0 + timedelta(seconds=360), lat=1.0, lon=2.0, speed_mph=60.0))
    topic, raw = svc.van_state(T0 + timedelta(seconds=361))
    assert topic == VAN_STATE_TOPIC
    payload = json.loads(raw)
    assert payload["trip_mi"] == 6.0
    assert payload["ignition"] is True
    assert payload["van_battery_v"] == 14.1
    assert payload["obd_ts"] == "2026-11-21T18:06:00Z"
    assert payload["gps_ts"] == "2026-11-21T18:06:00Z"
