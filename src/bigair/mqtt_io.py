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
from typing import TYPE_CHECKING

import aiomqtt
from loguru import logger

from bigair.ha_discovery import (
    AVAILABILITY_TOPIC,
    FILL_RESPONSE_TOPIC,
    FILL_RESULT_TOPIC,
    FUEL_STATE_TOPIC,
    MANUAL_FILL_TOPIC,
    OFFLINE,
    ONLINE,
    discovery_messages,
)

if TYPE_CHECKING:
    from collections.abc import Callable


async def run_mqtt(
    host: str,
    port: int,
    state_payload: Callable[[], str],
    on_command: Callable[[str, bytes], object],
    drain_outbox: Callable[[], list[tuple[str, str]]] = list,
    extra_states: Callable[[], list[tuple[str, str]]] = list,
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
                await client.subscribe(FILL_RESPONSE_TOPIC, qos=1)

                async def publisher(c: aiomqtt.Client = client) -> None:
                    while True:
                        await c.publish(FUEL_STATE_TOPIC, state_payload(), qos=0, retain=True)
                        for topic, payload in extra_states():  # e.g. the van state (#64)
                            await c.publish(topic, payload, qos=0, retain=True)
                        await asyncio.sleep(publish_every_s)

                async def outbox_sender(c: aiomqtt.Client = client) -> None:
                    while True:
                        for topic, payload in drain_outbox():
                            # Fill results are retained so HA's "Last fill-up" survives restarts.
                            retain = topic == FILL_RESULT_TOPIC
                            await c.publish(topic, payload, qos=1, retain=retain)
                        await asyncio.sleep(0.5)

                async def commands(c: aiomqtt.Client = client) -> None:
                    async for message in c.messages:
                        raw = message.payload
                        data = raw if isinstance(raw, bytes) else str(raw or "").encode()
                        on_command(str(message.topic), data)

                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(publisher())
                        tg.create_task(outbox_sender())
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


async def read_retained(host: str, port: int, topic: str, timeout_s: float = 5.0) -> bytes | None:
    """One retained message from ``topic``, or None if nothing arrives in time."""
    async with aiomqtt.Client(host, port) as client:
        await client.subscribe(topic)
        try:
            async with asyncio.timeout(timeout_s):
                async for msg in client.messages:
                    payload = msg.payload
                    return payload if isinstance(payload, bytes) else str(payload).encode()
        except TimeoutError:
            return None
    return None
