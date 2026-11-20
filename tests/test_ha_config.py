"""Guards for Home Assistant YAML that HA's own config check doesn't catch."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

HA_DIR = Path(__file__).parents[1] / "homeassistant"
YAML_FILES = sorted(p for p in HA_DIR.rglob("*.yaml") if "secrets" not in p.name)


class _HaLoader(yaml.SafeLoader):
    """SafeLoader that tolerates HA tags (!secret, !include, …) without resolving them."""


def _any_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> str:
    return f"<{suffix}>"


_HaLoader.add_multi_constructor("!", _any_tag)


def _load(path: Path) -> object:
    return yaml.load(path.read_text(), Loader=_HaLoader)


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: str(p.relative_to(HA_DIR)))
def test_statestream_never_publishes_everything(path: Path) -> None:
    """An empty include filter makes mqtt_statestream publish EVERY entity (verified on
    HA 2026.11). It may only exist with an explicit, non-empty include list."""
    doc = _load(path) or {}
    if not isinstance(doc, dict) or "mqtt_statestream" not in doc:
        return
    include = (doc["mqtt_statestream"] or {}).get("include") or {}
    assert any(include.get(k) for k in ("entities", "domains", "entity_globs")), (
        f"{path.name}: mqtt_statestream needs a non-empty include list"
    )


def _fill_up() -> dict[str, object]:
    doc = _load(HA_DIR / "packages" / "fill_up.yaml")
    assert isinstance(doc, dict)
    return doc


def test_no_phone_name_in_the_public_repo() -> None:
    """The notify action names Dan's phone; it must come from secrets.yaml."""
    for path in YAML_FILES:
        if path.name.endswith(".example"):
            continue
        assert "notify.mobile_app_" not in path.read_text(), path.name


def test_fill_up_scripts_exist() -> None:
    scripts = _fill_up()["script"]
    assert isinstance(scripts, dict)
    assert {"log_fill_up", "submit_fill_up_form"} <= set(scripts)


def test_fill_up_topics_match_bigair() -> None:
    from bigair.ha_discovery import FILL_RESPONSE_TOPIC, FILL_RESULT_TOPIC

    text = (HA_DIR / "packages" / "fill_up.yaml").read_text()
    assert FILL_RESPONSE_TOPIC in text
    assert FILL_RESULT_TOPIC in text
