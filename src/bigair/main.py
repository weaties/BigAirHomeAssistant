"""CLI entry point — wiring only, no business logic."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bigair.fuel_plan import Station


def _display(args: argparse.Namespace) -> int:
    import threading

    from loguru import logger

    from bigair.display_render import GaugeState
    from bigair.display_server import (
        debug_file_reader,
        gauge_file_reader,
        make_preview_server,
        make_server,
    )

    # Live numbers come from the fuel service's gauge feed (#17). Without one the
    # gauge truthfully shows "NO FUEL DATA" (a wrong fuel number is worse than none).
    gauge_path = os.environ.get("DISPLAY_GAUGE_FILE")

    def no_data() -> GaugeState:
        return GaugeState(tank_gal=None, range_mi=None)

    state = gauge_file_reader(Path(gauge_path)) if gauge_path else no_data

    # DISPLAY_DEBUG_FILE (host JSON, deploy/host/bigair-debug.py) adds Pi-supply
    # readings to the Van screen (#62).
    debug_path = os.environ.get("DISPLAY_DEBUG_FILE")
    debug_fn = debug_file_reader(Path(debug_path)) if debug_path else lambda: None
    tz = os.environ.get("TZ", "UTC")
    if args.preview_port:
        # Keyless browser preview; Compose publishes it on loopback only (Decision #37).
        preview = make_preview_server(
            host=args.host, port=args.preview_port, state_fn=state, tz=tz, debug_fn=debug_fn
        )
        threading.Thread(target=preview.serve_forever, daemon=True).start()
        logger.info("dash preview server on {}:{}", args.host, args.preview_port)
    server = make_server(
        host=args.host,
        port=args.port,
        api_key=os.environ.get("DISPLAY_API_KEY", ""),
        state_fn=state,
        refresh_s=int(os.environ.get("DISPLAY_REFRESH_SECONDS", "60")),
        full_refresh_s=int(os.environ.get("DISPLAY_FULL_REFRESH_SECONDS", "600")),
        tz=tz,
        debug_fn=debug_fn,
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
                    extra_states=lambda: [svc.van_state(datetime.now(UTC))],
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


def _fuel_trip_live(args: argparse.Namespace) -> tuple[float, tuple[Station, ...]]:
    """Route, stations and prices from Google + EIA (#71)."""
    from datetime import UTC, datetime

    from loguru import logger

    from bigair import eia_io, google_io
    from bigair.fuel_sources import (
        build_stations,
        collect_candidates,
        decode_eia,
        decode_route,
        drift_per_day,
        mirror_return,
        parse_discounts,
    )

    key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not key:
        raise SystemExit("set GOOGLE_MAPS_API_KEY (Routes API + Places API (New) enabled)")
    route = decode_route(google_io.compute_route(key, args.origin, args.to, args.via or []))
    found = collect_candidates(
        route, lambda seg, tok, q, typ: google_io.search_along(key, seg, tok, q, typ)
    )
    try:
        series = decode_eia(eia_io.fetch_diesel(os.environ.get("EIA_API_KEY", "DEMO_KEY")))
        drifts = {area: drift_per_day(v) for area, v in series.items()}
    except OSError as e:  # trend is a small term; plan without it rather than fail
        logger.warning("EIA trend unavailable ({}); planning without price drift", e)
        drifts = {}
    now = datetime.now(UTC)
    depart = datetime.fromisoformat(args.depart).astimezone(UTC) if args.depart else now
    stations = build_stations(
        found.candidates, route, now, depart, drifts, parse_discounts(args.discount), args.max_age_h
    )
    if found.unpriced:
        names = ", ".join(
            f"{u.name}{', ' + u.state if u.state else ''} (mi {u.mile:.0f})" for u in found.unpriced
        )
        print(f"Open Roads stations Google has no price for (check the app): {names}\n")
    if args.round_trip:
        return 2 * route.total_mi + args.local_mi, mirror_return(
            stations, route.total_mi, args.local_mi
        )
    return route.total_mi, stations


def _fuel_plan(args: argparse.Namespace) -> int:
    import asyncio
    import json

    from bigair.fuel_plan import (
        PlanInput,
        PlanRefusedError,
        default_home_price,
        format_options,
        naive_plan,
        plan_options,
        trip_from_dict,
        trip_to_dict,
    )

    if args.trip:
        route_mi, stations, home = trip_from_dict(json.loads(Path(args.trip).read_text()))
    elif args.origin and args.to:
        route_mi, stations = _fuel_trip_live(args)
        home = default_home_price(stations, route_mi)
        if args.save:
            Path(args.save).write_text(json.dumps(trip_to_dict(route_mi, stations, home), indent=1))
    else:
        print("give a trip file, or --from and --to")  # CLI output
        return 2

    tank, mpg, calibrating, stale = args.tank_gal, args.mpg, args.calibrating, False
    if args.live:
        from bigair.fuel_sources import live_fuel_from_state
        from bigair.ha_discovery import FUEL_STATE_TOPIC
        from bigair.mqtt_io import read_retained

        payload = asyncio.run(read_retained(args.mqtt_host, 1883, FUEL_STATE_TOPIC))
        if payload is None:
            print(f"no fuel state from {args.mqtt_host}")  # CLI output
            return 2
        live = live_fuel_from_state(payload)
        tank = live.tank_gal if tank is None else tank
        mpg = live.mpg if mpg is None else mpg
        calibrating, stale = calibrating or live.calibrating, live.stale
        if live.fill_pending:
            print("note: a fill is pending, so the tank reads low until it's confirmed")
    if tank is None or mpg is None:
        print("need --tank-gal and --mpg, or --live")  # CLI output
        return 2

    inp = PlanInput(
        route_mi=route_mi,
        stations=stations,
        start_gal=tank,
        mpg=mpg,
        capacity_gal=args.capacity,
        stop_penalty_usd=args.stop_penalty,
        end_value_per_gal=home if args.home_price is None else args.home_price,
        calibrating=calibrating,
        stale=stale,
    )
    reserves = tuple(float(r) for r in args.reserves.split(","))
    try:
        text = format_options(inp, plan_options(inp, reserves), naive_plan(inp))
    except PlanRefusedError as e:
        print(f"refused: {e}")  # CLI output, not operational logging
        return 2
    print(text)  # CLI output, not operational logging
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bigair", description="Van telemetry services")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")
    disp = sub.add_parser("display", help="serve the dash fuel gauge frame (eink-screen firmware)")
    disp.add_argument("--host", default="0.0.0.0")  # noqa: S104 — van LAN; Bearer key required
    disp.add_argument("--port", type=int, default=int(os.environ.get("DISPLAY_PORT", "8090")))
    disp.add_argument(
        "--preview-port", type=int, default=0, help="keyless browser preview; 0 = off"
    )
    sub.add_parser("run", help="run the fuel service (WiCAN → fuel model → HA/MQTT, InfluxDB)")
    fp = sub.add_parser("fuel-plan", help="where to buy fuel along a route, per reserve depth")
    fp.add_argument("trip", nargs="?", help="trip JSON (route_mi, stations[]); or use --from/--to")
    fp.add_argument("--from", dest="origin", help="live: origin address (Google Routes)")
    fp.add_argument("--to", help="live: destination address")
    fp.add_argument("--via", action="append", help="live: waypoint (repeatable)")
    fp.add_argument("--round-trip", action="store_true", help="live: out and back the same way")
    fp.add_argument("--local-mi", type=float, default=20.0, help="miles driven at the far end")
    fp.add_argument("--depart", help="ISO time of departure (default now), for the price trend")
    fp.add_argument(
        "--discount",
        default="loves=0.30,ta=0.30,petro=0.30",
        help="member discount $/gal by brand (Open Roads); 'none' to disable",
    )
    fp.add_argument("--max-age-h", type=float, default=72.0, help="ignore older station prices")
    fp.add_argument("--save", help="live: write the trip file for re-planning offline")
    fp.add_argument("--live", action="store_true", help="tank and mpg from bigair over MQTT")
    fp.add_argument("--mqtt-host", default=os.environ.get("BIGAIR_MQTT_HOST", "bigair-ha"))
    fp.add_argument("--tank-gal", type=float, help="fuel on board at the start")
    fp.add_argument("--mpg", type=float, help="expected mpg (be conservative)")
    fp.add_argument("--calibrating", action="store_true", help="tank still calibrating: lower mpg")
    fp.add_argument(
        "--reserves", default="150,100,75,50", help="reserve depths to compare, in miles"
    )
    fp.add_argument("--capacity", type=float, default=47.0)
    fp.add_argument("--stop-penalty", type=float, default=10.0, help="$ of hassle per stop")
    fp.add_argument("--home-price", type=float, help="$/gal value of fuel left at the end")
    args = parser.parse_args(argv)
    if args.version:
        from importlib.metadata import version

        print(version("bigair"))  # CLI output, not operational logging
        return 0
    if args.command == "display":
        return _display(args)
    if args.command == "run":
        return _run(args)
    if args.command == "fuel-plan":
        return _fuel_plan(args)
    return 0
