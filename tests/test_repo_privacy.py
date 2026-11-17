"""This repo is public: runtime state written by services must never be committable.

Home Assistant writes auth tokens, device registrations and its database into
its /config dir, which Compose bind-mounts from homeassistant/.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

MUST_BE_IGNORED = [
    "homeassistant/secrets.yaml",
    "homeassistant/.storage/auth",
    "homeassistant/.storage/core.config_entries",
    "homeassistant/home-assistant_v2.db",
    "homeassistant/home-assistant_v2.db-wal",
    "homeassistant/home-assistant.log",
    "homeassistant/home-assistant.log.1",
    "homeassistant/.HA_VERSION",
    "homeassistant/deps/x",
    "homeassistant/tts/x",
    "homeassistant/.cloud/x",
    "esphome/secrets.yaml",
    "deploy/.env",
    "deploy/volumes/influxdb/x",
    "data/fills.csv",
]


@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_runtime_and_secret_paths_are_gitignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT, check=False
    )
    assert result.returncode == 0, f"{path} is not gitignored"
