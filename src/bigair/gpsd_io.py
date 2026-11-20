"""Edge: gpsd JSON client (#14). gpsd runs on the Pi host (Decision #28)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

from loguru import logger

from bigair.gps_logger import fix_from_tpv

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bigair.signals import GpsFix

WATCH = b'?WATCH={"enable":true,"json":true};\n'


async def gpsd_fixes(
    host: str = "127.0.0.1", port: int = 2947, retry_s: float = 3.0
) -> AsyncIterator[GpsFix]:
    """Yield a GpsFix for every TPV report with a 2D/3D fix, reconnecting forever."""
    while True:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, conn = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
            writer = conn
            conn.write(WATCH)
            await conn.drain()
            logger.info("gpsd connected at {}:{}", host, port)
            while line := await reader.readline():
                try:
                    report = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(report, dict) and (fix := fix_from_tpv(report)) is not None:
                    yield fix
            logger.warning("gpsd closed the connection")
        except (OSError, TimeoutError) as e:
            logger.debug("gpsd unavailable: {}", e)
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        await asyncio.sleep(retry_s)
