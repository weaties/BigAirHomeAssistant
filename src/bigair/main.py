"""CLI entry point — wiring only, no business logic."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _display(args: argparse.Namespace) -> int:
    from loguru import logger

    from bigair.display_render import GaugeState
    from bigair.display_server import debug_file_reader, gauge_file_reader, make_server

    # Live numbers come from the fuel service's gauge feed (#17). Without one the
    # gauge truthfully shows "NO FUEL DATA" (a wrong fuel number is worse than none).
    gauge_path = os.environ.get("DISPLAY_GAUGE_FILE")

    def no_data() -> GaugeState:
        return GaugeState(tank_gal=None, range_mi=None)

    state = gauge_file_reader(Path(gauge_path)) if gauge_path else no_data

    # Drive-testing aid: DISPLAY_DEBUG_FILE (host JSON, deploy/host/bigair-debug.py)
    # replaces the sparkline with Pi-supply and van-battery readings.
    debug_path = os.environ.get("DISPLAY_DEBUG_FILE")
    debug = {"debug_fn": debug_file_reader(Path(debug_path))} if debug_path else {}
    server = make_server(
        host=args.host,
        port=args.port,
        api_key=os.environ.get("DISPLAY_API_KEY", ""),
        state_fn=state,
        refresh_s=int(os.environ.get("DISPLAY_REFRESH_SECONDS", "60")),
        full_refresh_s=int(os.environ.get("DISPLAY_FULL_REFRESH_SECONDS", "600")),
        tz=os.environ.get("TZ", "UTC"),
        **debug,
    )
    logger.info("dash display server on {}:{}", args.host, args.port)
    server.serve_forever()
    return 0


def _run(args: argparse.Namespace) -> int:
    import asyncio
    import signal
    from datetime import UTC, datetime

    from loguru import logger

    from bigair.clock import kernel_clock_synced
    from bigair.gpsd_io import gpsd_fixes
    from bigair.influx_io import InfluxWriter
    from bigair.mqtt_io import run_mqtt
    from bigair.service import FuelService, ServiceConfig
    from bigair.wican_elm import poll_samples

    env = os.environ
    svc = FuelService.start(
        ServiceConfig(
            state_path=Path(env.get("BIGAIR_STATE_FILE", "/data/fuel_state.json")),
            capacity_gal=float(env.get("TANK_CAPACITY_GAL", "47")),
            prior_mpg=float(env.get("PRIOR_MPG", "15")),
        )
    )
    token = env.get("INFLUX_BIGAIR_TOKEN", "")
    writer = (
        InfluxWriter(
            env.get("INFLUX_URL", "http://influxdb:8086"),
            org=env.get("INFLUX_ORG", "bigair"),
            token=token,
            clock_ok=kernel_clock_synced,  # chrony via the kernel's sync flag (#22)
        )
        if token
        else None
    )
    if writer is None:
        logger.warning("INFLUX_BIGAIR_TOKEN not set: not writing history")

    async def ingest() -> None:
        async for sample in poll_samples(
            env.get("WICAN_HOST", "192.168.8.106"), int(env.get("WICAN_PORT", "35000"))
        ):
            cand = svc.handle_sample(sample)  # prompts go out through svc's outbox
            if cand is not None:
                logger.info("fill candidate: ~{:.1f} gal", cand.est_gallons)
            if writer is not None:
                for point in svc.influx_points(sample.ts):
                    writer.add(point)

    gauge_path = env.get("BIGAIR_GAUGE_FILE")

    async def gauge_writer() -> None:
        from bigair.state_file import write_state

        while gauge_path:
            try:
                write_state(Path(gauge_path), svc.gauge_feed(datetime.now(UTC)), durable=False)
            except OSError as e:
                logger.warning("gauge feed write failed: {}", e)
            await asyncio.sleep(5)

    async def gps() -> None:
        async for fix in gpsd_fixes(
            env.get("GPSD_HOST", "127.0.0.1"), int(env.get("GPSD_PORT", "2947"))
        ):
            point = svc.handle_fix(fix)
            if point is not None and writer is not None:
                writer.add(point)

    async def influx_flusher() -> None:
        held = False
        while writer is not None:
            await asyncio.sleep(10)
            synced = kernel_clock_synced()
            if synced == held:  # log transitions only
                held = not synced
                if held:
                    logger.warning(
                        "clock not synced: holding Influx writes ({} buffered)", writer.pending
                    )
                else:
                    logger.info("clock synced: writing {} buffered points", writer.pending)
            for point in svc.drain_points():  # fill-ups and other events
                writer.add(point)
            try:
                await asyncio.to_thread(writer.flush)
            except Exception as e:  # noqa: BLE001 — never let history kill the fuel loop
                logger.warning("influx flush failed: {}", e)

    async def main_async() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        tasks = [
            asyncio.create_task(ingest()),
            asyncio.create_task(gps()),
            asyncio.create_task(influx_flusher()),
            asyncio.create_task(gauge_writer()),
            asyncio.create_task(
                run_mqtt(
                    env.get("MQTT_HOST", "mosquitto"),
                    int(env.get("MQTT_PORT", "1883")),
                    state_payload=lambda: svc.state_payload(datetime.now(UTC)),
                    on_command=lambda t, p: svc.handle_command(t, p, datetime.now(UTC)),
                    drain_outbox=svc.drain_outbox,
                )
            ),
        ]
        await stop.wait()
        logger.info("stopping: saving fuel state")
        svc.flush(datetime.now(UTC))
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            await asyncio.to_thread(writer.flush)

    asyncio.run(main_async())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bigair", description="Van telemetry services")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")
    disp = sub.add_parser("display", help="serve the dash fuel gauge frame (eink-screen firmware)")
    disp.add_argument("--host", default="0.0.0.0")  # noqa: S104 — van LAN; Bearer key required
    disp.add_argument("--port", type=int, default=int(os.environ.get("DISPLAY_PORT", "8090")))
    sub.add_parser("run", help="run the fuel service (WiCAN → fuel model → HA/MQTT, InfluxDB)")
    args = parser.parse_args(argv)
    if args.version:
        from importlib.metadata import version

        print(version("bigair"))  # CLI output, not operational logging
        return 0
    if args.command == "display":
        return _display(args)
    if args.command == "run":
        return _run(args)
    return 0
