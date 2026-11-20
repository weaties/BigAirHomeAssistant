"""Edge: MQTT session with Mosquitto (aiomqtt).

On every (re)connect: LWT `bigair/status` = offline (retained), publish
online, the HA discovery configs (retained) and subscribe to commands. Then
publish the fuel state every few seconds (retained, display copy only —
Decision #22) and fill candidates as they come. Reconnects forever; the fuel
model keeps integrating while the broker is away. On shutdown it publishes
offline itself, because a clean disconnect never fires the will.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import aiomqtt
from loguru import logger

from bigair.ha_discovery import (
    AVAILABILITY_TOPIC,
    FUEL_STATE_TOPIC,
    MANUAL_FILL_TOPIC,
    OFFLINE,
    ONLINE,
    discovery_messages,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from bigair.signals import FillCandidate

CANDIDATE_TOPIC = "bigair/fill_up/candidate"


def candidate_payload(c: FillCandidate) -> str:
    return json.dumps(
        {
            "fill_id": c.fill_id,
            "ts": c.ts.isoformat(),
            "est_gallons": round(c.est_gallons, 1),
            "odometer_mi": c.odometer_mi,
            "lat": c.lat,
            "lon": c.lon,
        }
    )


async def run_mqtt(
    host: str,
    port: int,
    state_payload: Callable[[], str],
    on_command: Callable[[str, bytes], object],
    candidates: asyncio.Queue[FillCandidate],
    publish_every_s: float = 5.0,
    retry_s: float = 5.0,
) -> None:
    will = aiomqtt.Will(AVAILABILITY_TOPIC, OFFLINE, qos=1, retain=True)
    while True:
        try:
            async with aiomqtt.Client(host, port, identifier="bigair", will=will) as client:
                logger.info("MQTT connected to {}:{}", host, port)
                await client.publish(AVAILABILITY_TOPIC, ONLINE, qos=1, retain=True)
                for topic, payload in discovery_messages():
                    await client.publish(topic, payload, qos=1, retain=True)
                await client.subscribe(MANUAL_FILL_TOPIC, qos=1)

                async def publisher(c: aiomqtt.Client = client) -> None:
                    while True:
                        await c.publish(FUEL_STATE_TOPIC, state_payload(), qos=0, retain=True)
                        await asyncio.sleep(publish_every_s)

                async def candidate_sender(c: aiomqtt.Client = client) -> None:
                    while True:
                        cand = await candidates.get()
                        await c.publish(CANDIDATE_TOPIC, candidate_payload(cand), qos=1)

                async def commands(c: aiomqtt.Client = client) -> None:
                    async for message in c.messages:
                        raw = message.payload
                        data = raw if isinstance(raw, bytes) else str(raw or "").encode()
                        on_command(str(message.topic), data)

                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(publisher())
                        tg.create_task(candidate_sender())
                        tg.create_task(commands())
                except asyncio.CancelledError:
                    # A clean disconnect never triggers the will, so say offline
                    # ourselves or HA keeps showing the last values as current.
                    with contextlib.suppress(aiomqtt.MqttError, TimeoutError):
                        await asyncio.wait_for(
                            client.publish(AVAILABILITY_TOPIC, OFFLINE, qos=1, retain=True), 2
                        )
                    raise
        except* aiomqtt.MqttError as eg:
            logger.warning("MQTT session ended: {}; retrying in {} s", eg.exceptions[0], retry_s)
        await asyncio.sleep(retry_s)
