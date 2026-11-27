"""The phone dash (#64) only references entities that exist."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from bigair.ha_discovery import DEVICE, discovery_messages

HA_DIR = Path(__file__).parents[1] / "homeassistant"
DASH = HA_DIR / "dashboards" / "van.yaml"


class _HaLoader(yaml.SafeLoader):
    pass


_HaLoader.add_multi_constructor("!", lambda loader, suffix, node: f"<{suffix}>")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _discovered() -> set[str]:
    """HA names discovered entities <domain>.<device name>_<entity name> (checked against
    the Pi's entity registry, HA 2026.11: object_id is not used)."""
    out = set()
    for topic, payload in discovery_messages():
        domain = topic.split("/")[1]
        out.add(f"{domain}.{_slug(f'{DEVICE["name"]} {json.loads(payload)["name"]}')}")
    return out


def _package_entities() -> set[str]:
    out = set()
    for path in (HA_DIR / "packages").glob("*.yaml"):
        doc = yaml.load(path.read_text(), Loader=_HaLoader) or {}
        for domain in ("input_number", "input_select", "input_text", "script"):
            out |= {f"{domain}.{k}" for k in doc.get(domain) or {}}
    return out


# Entities from integrations added in HA's UI, which git can't derive. Each id was read
# from the Pi's entity registry; renaming the device in HA changes it.
UI_ENTITIES = {
    "light.bed_lights",  # Tapo L930 strip, tplink integration (#68)
}


def _entity_refs(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("entity", "entity_id") and isinstance(value, str):
                found.add(value)
            elif key == "perform_action" and str(value).startswith("script."):
                found.add(str(value))
            else:
                found |= _entity_refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _entity_refs(item)
    return found


def _dash() -> dict[str, object]:
    doc = yaml.safe_load(DASH.read_text())
    assert isinstance(doc, dict)
    return doc


def test_dashboard_is_registered_from_git() -> None:
    config = yaml.load((HA_DIR / "configuration.yaml").read_text(), Loader=_HaLoader)
    dash = config["lovelace"]["dashboards"]["bigair-van"]
    assert dash["mode"] == "yaml"
    assert (HA_DIR / dash["filename"]) == DASH
    assert "-" in "bigair-van"  # HA requires a hyphen in dashboard url paths


def test_existing_entity_ids_follow_the_naming_rule() -> None:
    """Spot-check the slug rule against ids HA actually created on the Pi."""
    ids = _discovered()
    assert "sensor.bigair_van_fuel_tank" in ids
    assert "sensor.bigair_van_fuel_economy_30_s" in ids
    assert "binary_sensor.bigair_van_fill_up_pending" in ids
    assert "button.bigair_van_filled_to_full" in ids


def test_every_referenced_entity_exists() -> None:
    known = _discovered() | _package_entities() | UI_ENTITIES
    refs = _entity_refs(_dash())
    assert refs, "the dashboard references no entities?"
    assert refs <= known, sorted(refs - known)


def test_no_personal_entities() -> None:
    """Only bigair's and the fill-up package's entities: no phone, person or tracker."""
    for ref in _entity_refs(_dash()):
        assert not ref.startswith(("person.", "device_tracker.", "notify.")), ref


def test_trip_view_embeds_the_planner() -> None:
    (trip,) = [v for v in _dash()["views"] if v["path"] == "trip"]
    assert trip["type"] == "panel"
    (card,) = trip["cards"]
    assert card["type"] == "iframe"
    assert card["url"].endswith(":8092/")


def test_every_view_is_a_sections_view_with_a_path() -> None:
    views = _dash()["views"]
    assert isinstance(views, list)
    assert [v["path"] for v in views] == ["drive", "fill", "history", "trip"]
    assert all(v["type"] == "sections" for v in views if v["path"] != "trip")
    # The pending-fill alert links to the Fill-up view by its path.
    assert "/bigair-van/fill" in DASH.read_text()
