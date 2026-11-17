"""Guard provisioned dashboard JSON against drift (see /grafana-dashboards)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DASH_DIR = Path(__file__).parents[1] / "grafana" / "provisioning" / "dashboards" / "json"
KNOWN_DATASOURCES = {"influx-van-raw", "influx-van-history"}
FILES = sorted(DASH_DIR.glob("*.json"))


def _datasource_uids(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        ds = node.get("datasource")
        if isinstance(ds, dict) and "uid" in ds:
            found.append(ds["uid"])
        for value in node.values():
            found += _datasource_uids(value)
    elif isinstance(node, list):
        for item in node:
            found += _datasource_uids(item)
    return found


def test_there_are_dashboards() -> None:
    assert FILES


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_dashboard_is_provisionable(path: Path) -> None:
    text = path.read_text()
    assert "${DS_" not in text, "export with 'sharing externally' OFF"
    dash = json.loads(text)
    assert dash["uid"] == path.stem
    assert dash["uid"].startswith("bigair-")
    assert dash["id"] is None
    uids = _datasource_uids(dash)
    assert uids, "panels must reference a datasource by uid"
    assert set(uids) <= KNOWN_DATASOURCES


def test_uids_are_unique() -> None:
    uids = [json.loads(p.read_text())["uid"] for p in FILES]
    assert len(uids) == len(set(uids))
