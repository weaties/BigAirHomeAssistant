"""Edge: poll the WiCAN's ELM327 interface over TCP (Decision #33).

The WiCAN-OBD-PRO serves ELM327 emulation on TCP 35000 on the van network.
Read-only mode 01 requests, physically addressed to the engine ECU (7E0).
While the engine is off the WiCAN sleeps and refuses connections, so the
poller just keeps retrying; it never invents samples.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger

from bigair.obd_decode import POLLED_PIDS, sample_from_responses

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bigair.signals import ObdSample

# Echo/linefeeds/spaces/headers off, CAN 11-bit 500k (as scanned), engine ECU,
# ~200 ms response timeout.
INIT_COMMANDS = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP6", "ATSH7E0", "ATST32")


class ElmClient:
    def __init__(self, host: str, port: int, timeout_s: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout_s
        )
        for cmd in INIT_COMMANDS:
            await self.query(cmd)

    async def query(self, cmd: str) -> str:
        """Send one command; return the reply text up to the '>' prompt."""
        if self._reader is None or self._writer is None:
            raise ConnectionError("not connected")
        self._writer.write((cmd + "\r").encode())
        await self._writer.drain()
        try:
            raw = await asyncio.wait_for(self._reader.readuntil(b">"), self.timeout_s)
        except asyncio.IncompleteReadError as e:
            raise ConnectionError("adapter closed the connection") from e
        return raw[:-1].decode(errors="replace").replace("\r", "\n").strip()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
        self._reader = self._writer = None


async def poll_samples(
    host: str, port: int, interval_s: float = 1.0, retry_s: float = 3.0
) -> AsyncIterator[ObdSample]:
    """Yield one ObdSample per polling round, reconnecting as needed, forever."""
    client = ElmClient(host, port)
    connected = False
    try:
        while True:
            try:
                if not connected:
                    await client.connect()
                    connected = True
                    logger.info("WiCAN connected at {}:{}", host, port)
                started = asyncio.get_running_loop().time()
                responses = {pid: await client.query("01" + pid) for pid in POLLED_PIDS}
                yield sample_from_responses(datetime.now(UTC), responses)
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.0, interval_s - elapsed))
            except (OSError, ConnectionError, TimeoutError) as e:
                if connected:
                    logger.warning("WiCAN connection lost: {}", e)
                connected = False
                await client.close()
                await asyncio.sleep(retry_s)
    finally:
        await client.close()
