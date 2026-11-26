"""Edge: Victron Instant Readout advertisements over BLE (#109).

bleak talks to the host's BlueZ over D-Bus (the container mounts /run/dbus). Only
advertisements from the configured MACs with Victron's company id are passed on,
stamped with wall and monotonic time. Decoding happens in victron_ble.py. HA's
Bluetooth integration stays off, and the XGPS160 shares the radio (Decision #28).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bleak import BleakScanner
from loguru import logger

from bigair.victron_ble import COMPANY_ID

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Collection

    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

Advert = tuple[str, bytes, datetime, float]  # (MAC, manufacturer data, ts UTC, monotonic s)


async def victron_adverts(
    macs: Collection[str], retry_s: float = 15.0, queue_max: int = 200
) -> AsyncGenerator[Advert]:
    """Yield Victron advertisements from `macs` (upper-case), rescanning after errors."""
    wanted = {m.upper() for m in macs}
    queue: asyncio.Queue[Advert] = asyncio.Queue(maxsize=queue_max)

    def seen(device: BLEDevice, adv: AdvertisementData) -> None:
        data = adv.manufacturer_data.get(COMPANY_ID)
        mac = device.address.upper()
        if data is None or mac not in wanted:
            return
        with contextlib.suppress(asyncio.QueueFull):  # drop rather than block BlueZ
            queue.put_nowait((mac, bytes(data), datetime.now(UTC), time.monotonic()))

    while True:
        try:
            async with BleakScanner(detection_callback=seen):
                logger.info("Victron BLE scan started for {} devices", len(wanted))
                while True:
                    yield await queue.get()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — BlueZ/D-Bus errors vary; keep retrying
            logger.warning("Victron BLE scan failed: {}; retrying in {:.0f} s", e, retry_s)
        await asyncio.sleep(retry_s)
