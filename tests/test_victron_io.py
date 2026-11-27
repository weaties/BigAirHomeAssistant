"""Victron BLE edge (#109), with bleak's scanner faked at the import site."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from bigair import victron_io

MAC = "F1:DB:C7:00:00:01"  # synthetic


class FakeScanner:
    adverts: list[tuple[str, dict[int, bytes]]] = []
    fail_first = False
    starts = 0

    def __init__(self, detection_callback: Callable[[object, object], None]) -> None:
        self.cb = detection_callback

    async def __aenter__(self) -> FakeScanner:
        FakeScanner.starts += 1
        if FakeScanner.fail_first and FakeScanner.starts == 1:
            raise OSError("org.bluez.Error.NotReady")
        for mac, mfr in self.adverts:
            self.cb(SimpleNamespace(address=mac), SimpleNamespace(manufacturer_data=mfr))
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeScanner]:
    FakeScanner.starts = 0
    FakeScanner.fail_first = False
    monkeypatch.setattr(victron_io, "BleakScanner", FakeScanner)
    return FakeScanner


async def test_only_wanted_victron_adverts_pass() -> None:
    FakeScanner.adverts = [
        ("AA:BB:CC:DD:EE:FF", {0x02E1: b"\x10other"}),  # not ours
        (MAC.lower(), {0x004C: b"apple"}),  # ours, but not Victron data
        (MAC.lower(), {0x02E1: b"\x10ours"}),
    ]
    before = time.monotonic()
    gen = victron_io.victron_adverts([MAC.lower()], scan_s=0.5, pause_s=0.01)
    mac, data, ts, mono = await anext(gen)
    await gen.aclose()
    assert (mac, data) == (MAC, b"\x10ours")
    assert ts.tzinfo is not None
    assert before <= mono <= time.monotonic()


async def test_a_failed_scan_start_is_retried() -> None:
    FakeScanner.fail_first = True
    FakeScanner.adverts = [(MAC, {0x02E1: b"\x10ours"})]
    gen = victron_io.victron_adverts([MAC], scan_s=0.5, pause_s=0.01, retry_s=0.01)
    assert (await anext(gen))[1] == b"\x10ours"
    await gen.aclose()
    assert FakeScanner.starts == 2


async def test_scans_in_bursts_so_the_gps_keeps_the_radio() -> None:
    """Each window opens the scanner afresh; between windows it's closed."""
    FakeScanner.adverts = [(MAC, {0x02E1: b"\x10ours"})]
    gen = victron_io.victron_adverts([MAC], scan_s=0.05, pause_s=0.01)
    for _ in range(3):
        assert (await anext(gen))[1] == b"\x10ours"
    await gen.aclose()
    assert FakeScanner.starts == 3
