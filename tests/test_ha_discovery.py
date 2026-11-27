"""HA MQTT discovery payloads and availability (#13). Rules: /home-assistant-config."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from bigair.ha_discovery import (
    AVAILABILITY_TOPIC,
    ELECTRICAL_STATE_TOPIC,
    FUEL_STATE_TOPIC,
    MANUAL_FILL_TOPIC,
    OFFLINE,
    ONLINE,
    VAN_STATE_TOPIC,
    discovery_messages,
    fuel_state_payload,
)

TS = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)


def _configs() -> dict[str, dict[str, object]]:
    return {topic: json.loads(payload) for topic, payload in discovery_messages()}


def test_every_message_is_a_retained_discovery_config_topic() -> None:
    for topic in _configs():
        parts = topic.split("/")
        assert parts[0] == "homeassistant"
        assert parts[1] in {"sensor", "binary_sensor", "button"}
        assert parts[2] == "bigair"
        assert parts[-1] == "config"


def test_core_fuel_sensors_are_published() -> None:
    ids = {c["object_id"] for c in _configs().values()}
    assert {"tank_gal", "range_mi", "mpg_rolling", "mpg_instant_30s", "mpg_since_full"} <= ids
    assert "fuel_stale" in ids


def test_units_and_classes() -> None:
    by_id = {c["object_id"]: c for c in _configs().values()}
    assert by_id["tank_gal"]["unit_of_measurement"] == "gal"
    assert by_id["tank_gal"]["device_class"] == "volume_storage"
    assert by_id["range_mi"]["unit_of_measurement"] == "mi"
    assert by_id["range_mi"]["device_class"] == "distance"
    assert by_id["mpg_rolling"]["unit_of_measurement"] == "mpg"
    assert by_id["fuel_stale"]["device_class"] == "problem"


def test_unique_ids_and_one_device() -> None:
    configs = list(_configs().values())
    uids = [c["unique_id"] for c in configs]
    assert len(set(uids)) == len(uids)
    assert all(str(u).startswith("bigair_") for u in uids)
    assert len({json.dumps(c["device"], sort_keys=True) for c in configs}) == 1


def test_every_entity_goes_unavailable_when_bigair_stops() -> None:
    for c in _configs().values():
        avail = c["availability"]
        assert isinstance(avail, list)
        assert {
            "topic": AVAILABILITY_TOPIC,
            "payload_available": ONLINE,
            "payload_not_available": OFFLINE,
        } in avail


def test_tank_and_range_go_unavailable_when_stale() -> None:
    """A wrong fuel number is worse than no number (CLAUDE.md)."""
    by_id = {c["object_id"]: c for c in _configs().values()}
    for oid in ("tank_gal", "range_mi"):
        assert by_id[oid]["availability_mode"] == "all"
        topics = {a["topic"] for a in by_id[oid]["availability"]}  # type: ignore[index]
        assert FUEL_STATE_TOPIC in topics


def test_state_topics_and_templates_point_at_the_payload_keys() -> None:
    payload = json.loads(fuel_state_payload(TS, 39.95, 595.0, 14.894, None, None, False))
    for c in _configs().values():
        if "command_topic" in c or c["object_id"] == "last_fill_up":
            continue  # the button has no state; last fill-up has its own topic
        if c["state_topic"] in (VAN_STATE_TOPIC, ELECTRICAL_STATE_TOPIC):
            continue  # van: test_ha_van_state.py; electrical: test below
        assert c["state_topic"] == FUEL_STATE_TOPIC
        key = {
            "fuel_stale": "stale",
            "fuel_calibrating": "calibrating",
            "fill_pending": "fill_pending",
        }.get(str(c["object_id"]), str(c["object_id"]))
        assert key in payload


def test_fuel_state_payload_rounds_and_keeps_unknowns_null() -> None:
    payload = json.loads(fuel_state_payload(TS, 39.95123, 595.04, 14.8936, None, 13.2, True))
    assert payload == {
        "ts": "2026-06-01T15:00:00+00:00",
        "tank_gal": 39.95,
        "range_mi": 595.0,
        "mpg_rolling": 14.89,
        "mpg_instant_30s": None,
        "mpg_since_full": 13.2,
        "stale": True,
        "calibrating": False,
        "fill_pending": False,
    }


def test_calibrating_flag_is_a_binary_sensor() -> None:
    by_id = {c["object_id"]: c for c in _configs().values()}
    cal = by_id["fuel_calibrating"]
    assert cal["state_topic"] == FUEL_STATE_TOPIC
    assert "calibrating" in str(cal["value_template"])


def test_filled_to_full_button() -> None:
    topics = dict(discovery_messages())
    cfg = json.loads(topics["homeassistant/button/bigair/filled_to_full/config"])
    assert cfg["command_topic"] == MANUAL_FILL_TOPIC
    assert cfg["payload_press"] == "full"
    assert cfg["unique_id"] == "bigair_filled_to_full"


def test_last_fill_up_sensor_reads_the_result_topic() -> None:
    topics = dict(discovery_messages())
    cfg = json.loads(topics["homeassistant/sensor/bigair/last_fill_up/config"])
    assert cfg["state_topic"] == "bigair/fill_up/result"
    assert cfg["json_attributes_topic"] == "bigair/fill_up/result"


def test_electrical_sensors_read_the_trackers_keys() -> None:
    """#109: every Victron sensor's template key is in the tracker's payload."""
    from bigair.electrical import ElectricalTracker

    keys = set(ElectricalTracker.KEYS) - {"energy_estimated"}  # a flag, not a sensor
    configs = [c for c in _configs().values() if c.get("state_topic") == ELECTRICAL_STATE_TOPIC]
    assert {c["object_id"] for c in configs} == keys
    for c in configs:
        assert c["value_template"] == f"{{{{ value_json.{c['object_id']} }}}}"


def test_text_states_have_no_unit_or_state_class() -> None:
    c = next(c for c in _configs().values() if c.get("object_id") == "solar_state")
    assert "unit_of_measurement" not in c
    assert "state_class" not in c
