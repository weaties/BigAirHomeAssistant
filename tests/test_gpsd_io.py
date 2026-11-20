"""gpsd JSON client edge against a fake gpsd on localhost."""

from __future__ import annotations

import asyncio
import contextlib
import json

from bigair.gpsd_io import gpsd_fixes

TPV = {
    "class": "TPV",
    "mode": 3,
    "time": "2026-11-21T15:00:00.000Z",
    "lat": 39.5,
    "lon": -111.0,
    "speed": 10.0,
    "track": 90.0,
    "altMSL": 1500.0,
}


async def test_yields_fixes_from_tpv_and_skips_other_reports() -> None:
    seen: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'{"class":"VERSION","release":"3.25"}\n')
        seen.append(await reader.readline())  # the WATCH command
        for msg in ({"class": "SKY"}, {**TPV, "mode": 1}, TPV):
            writer.write((json.dumps(msg) + "\n").encode())
        await writer.drain()
        await asyncio.sleep(1)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    fixes = gpsd_fixes("127.0.0.1", port, retry_s=0.01)
    fix = await asyncio.wait_for(anext(fixes), 2)
    await fixes.aclose()
    server.close()
    assert fix.lat == 39.5 and fix.alt_m == 1500.0
    assert b"WATCH" in seen[0]


async def test_keeps_retrying_without_gpsd() -> None:
    fixes = gpsd_fixes("127.0.0.1", 1, retry_s=0.01)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(anext(fixes), 0.2)
    await fixes.aclose()
