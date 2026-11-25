"""HA delivers Claude's evening briefing (#94): topic and fields match bigair."""

from __future__ import annotations

from pathlib import Path

import yaml

from bigair.main import BRIEFING_TOPIC

PKG = Path(__file__).parents[1] / "homeassistant" / "packages" / "claude.yaml"


class _HaLoader(yaml.SafeLoader):
    pass


_HaLoader.add_multi_constructor("!", lambda loader, suffix, node: f"<{suffix}>")


def test_briefing_automation_listens_on_bigairs_topic() -> None:
    cfg = yaml.load(PKG.read_text(), Loader=_HaLoader)
    auto = cfg["automation"][0]
    assert auto["triggers"][0] == {"trigger": "mqtt", "topic": BRIEFING_TOPIC}
    action = auto["actions"][0]
    assert action["action"] == "<secret>"  # the phone comes from secrets.yaml
    data = action["data"]
    for field in ("title", "message"):
        assert f"payload_json.{field}" in data[field]
    assert "payload_json.url" in data["data"]["url"]
