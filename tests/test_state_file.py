"""Durable fuel state: atomic write, restore, crash safety (#9, Decision #22)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bigair.state_file import StateFileError, StateSaver, read_state, write_state

if TYPE_CHECKING:
    from pathlib import Path


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "fuel_state.json"
    write_state(path, {"a": 1.5})
    assert read_state(path) == {"a": 1.5}


def test_missing_file_reads_as_none(tmp_path: Path) -> None:
    assert read_state(tmp_path / "nope.json") is None


def test_crash_mid_write_keeps_the_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fuel_state.json"
    write_state(path, {"tank": 40.0})

    def power_cut(*_: object) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", power_cut)
    with pytest.raises(OSError, match="power cut"):
        write_state(path, {"tank": 39.0})
    monkeypatch.undo()
    assert read_state(path) == {"tank": 40.0}


def test_leftover_temp_files_are_ignored_and_replaced(tmp_path: Path) -> None:
    path = tmp_path / "fuel_state.json"
    write_state(path, {"tank": 40.0})
    (tmp_path / "fuel_state.json.tmp").write_text('{"tank": 3')  # torn write from a crash
    assert read_state(path) == {"tank": 40.0}
    write_state(path, {"tank": 39.0})
    assert read_state(path) == {"tank": 39.0}


def test_corrupt_file_raises_rather_than_guessing(tmp_path: Path) -> None:
    path = tmp_path / "fuel_state.json"
    path.write_text("{not json")
    with pytest.raises(StateFileError):
        read_state(path)


def test_file_is_plain_json(tmp_path: Path) -> None:
    path = tmp_path / "fuel_state.json"
    write_state(path, {"tank": 40.0})
    assert json.loads(path.read_text()) == {"tank": 40.0}


T0 = datetime(2026, 6, 1, tzinfo=UTC)


def test_saver_writes_at_most_every_interval(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    saver = StateSaver(path, interval_s=10.0)
    assert saver.save({"n": 1}, T0)
    assert not saver.save({"n": 2}, T0 + timedelta(seconds=9))
    assert read_state(path) == {"n": 1}
    assert saver.save({"n": 3}, T0 + timedelta(seconds=10))
    assert read_state(path) == {"n": 3}


def test_saver_force_writes_immediately(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    saver = StateSaver(path, interval_s=10.0)
    saver.save({"n": 1}, T0)
    assert saver.save({"n": 2}, T0 + timedelta(seconds=1), force=True)
    assert read_state(path) == {"n": 2}


def test_saver_skips_unchanged_state(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    saver = StateSaver(path, interval_s=10.0)
    saver.save({"n": 1}, T0)
    assert not saver.save({"n": 1}, T0 + timedelta(seconds=60))
