"""Durable local state file — the fuel model's source of truth across power cuts.

Decision #22: write to a temp file, fsync it, rename over the old file, fsync
the directory. A crash at any point leaves either the old or the new file,
never a torn one. Retained MQTT is a display copy only.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


class StateFileError(Exception):
    """The state file exists but can't be read. Callers show 'unknown', never a guess."""


def write_state(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace `path` with `data` as JSON."""
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    with tmp.open("w") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_state(path: Path) -> dict[str, Any] | None:
    """The saved state, or None if there's no file yet (fresh install)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise StateFileError(f"{path}: {e}") from e
    if not isinstance(data, dict):
        raise StateFileError(f"{path}: expected a JSON object")
    return data


class StateSaver:
    """Writes at most once per `interval_s` unless forced (fills), and only on change."""

    def __init__(self, path: Path, interval_s: float = 10.0) -> None:
        self.path = path
        self.interval_s = interval_s
        self._last_ts: datetime | None = None
        self._last_data: dict[str, Any] | None = None

    def save(self, data: dict[str, Any], now: datetime, force: bool = False) -> bool:
        """Write if forced, or if changed and the interval has passed. True if written."""
        if data == self._last_data:
            return False
        due = self._last_ts is None or (now - self._last_ts).total_seconds() >= self.interval_s
        if not (force or due):
            return False
        write_state(self.path, data)
        self._last_ts = now
        self._last_data = data
        return True
