"""WiCAN ELM327 TCP client and poller against a fake adapter on localhost."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from bigair.wican_elm import ElmClient, poll_samples

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

INIT = ["ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP6", "ATSH7E0", "ATST32"]


class FakeElm:
    """Answers like the WiCAN: echo off after ATE0, '>' prompt after each reply."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.seen: list[str] = []
        self.server: asyncio.Server | None = None
        self.drop_after: int | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buf = b""
        while True:
            chunk = await reader.read(64)
            if not chunk:
                break
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                cmd = line.decode().strip()
                self.seen.append(cmd)
                if self.drop_after is not None and len(self.seen) >= self.drop_after:
                    writer.close()
                    return
                reply = self.answers.get(cmd, "OK" if cmd.startswith("AT") else "NO DATA")
                writer.write(f"{reply}\r\r>".encode())
                await writer.drain()
        writer.close()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


@pytest.fixture
async def fake() -> AsyncIterator[FakeElm]:
    elm = FakeElm({"010D": "410D40", "015E": "415E004B", "012F": "412F3F", "ATZ": "ELM327 v2.3"})
    yield elm
    if elm.server:
        elm.server.close()


async def test_connect_initialises_the_adapter(fake: FakeElm) -> None:
    port = await fake.start()
    client = ElmClient("127.0.0.1", port)
    await client.connect()
    assert fake.seen == INIT
    assert await client.query("010D") == "410D40"
    await client.close()


async def test_poll_yields_decoded_samples(fake: FakeElm) -> None:
    port = await fake.start()
    samples = poll_samples("127.0.0.1", port, interval_s=0.01)
    s = await anext(samples)
    await samples.aclose()
    assert s.ignition
    assert s.speed_mph == pytest.approx(64 * 0.621371)
    assert s.fuel_level_pct == pytest.approx(24.706, abs=1e-3)


async def test_poll_reconnects_after_the_adapter_drops(fake: FakeElm) -> None:
    port = await fake.start()
    fake.drop_after = len(INIT) + 2  # drop mid-round on the first connection
    samples = poll_samples("127.0.0.1", port, interval_s=0.01, retry_s=0.01)
    task = asyncio.ensure_future(anext(samples))
    await asyncio.sleep(0.2)
    fake.drop_after = None
    s = await asyncio.wait_for(task, 2)
    await samples.aclose()
    assert s.ignition
    assert fake.seen.count("ATZ") >= 2


async def test_poll_keeps_retrying_while_the_adapter_sleeps() -> None:
    """Engine off: the WiCAN sleeps and refuses connections. No samples, no crash."""
    samples = poll_samples("127.0.0.1", 1, interval_s=0.01, retry_s=0.01)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(anext(samples), 0.2)
    await samples.aclose()


async def test_poll_reads_the_adapter_battery_voltage(fake: FakeElm) -> None:
    fake.answers["ATRV"] = "14.2V"
    port = await fake.start()
    samples = poll_samples("127.0.0.1", port, interval_s=0.01)
    s = await anext(samples)
    await samples.aclose()
    assert s.battery_v == pytest.approx(14.2)
