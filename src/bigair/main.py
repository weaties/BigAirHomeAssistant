"""CLI entry point — wiring only, no business logic."""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bigair.byways import ByWayIndex
    from bigair.claude_service import ClaudeService
    from bigair.fuel_sources import LiveFuel
    from bigair.travel_rules import TravelRules
    from bigair.trip_service import TripEdges


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


def _trip_edges(mqtt_host: str, gpsd: bool = True) -> TripEdges:
    """The real network edges for a trip plan (#71, #75)."""
    import asyncio
    from datetime import UTC, datetime

    from loguru import logger

    from bigair import eia_io, google_io, weather_io
    from bigair.byways import load_byways
    from bigair.dest_sources import load_ioverlander, load_public_land
    from bigair.fuel_sources import live_fuel_from_state
    from bigair.gpsd_io import current_fix
    from bigair.ha_discovery import FUEL_STATE_TOPIC
    from bigair.mqtt_io import read_retained
    from bigair.poi import load_pois
    from bigair.trip_service import TripEdges, TripError

    key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not key:
        raise TripError("GOOGLE_MAPS_API_KEY isn't set (Routes API + Places API (New))")
    eia_key = os.environ.get("EIA_API_KEY") or "DEMO_KEY"

    def fuel_state() -> LiveFuel | None:
        payload = asyncio.run(read_retained(mqtt_host, 1883, FUEL_STATE_TOPIC))
        return live_fuel_from_state(payload) if payload else None

    def position() -> tuple[float, float] | None:
        fix = asyncio.run(current_fix()) if gpsd else None
        return (fix.lat, fix.lon) if fix else None

    # POIs from the vault (#88): the Syncthing folder, mounted read-only.
    poi_dir = os.environ.get("POI_DIR", "")
    pois = (lambda: load_pois(Path(poi_dir))) if poi_dir else None
    # Scenic byways for slow mode (#100): a local GeoJSON of byway lines, loaded once.
    byways_file = os.environ.get("BYWAYS_FILE", "")
    byway_cache: dict[str, Any] = {}

    def byways() -> ByWayIndex | None:
        if "idx" not in byway_cache:
            idx, problems = load_byways(Path(byways_file))
            for p in problems:
                logger.warning("{}", p)
            byway_cache["idx"] = idx
        return byway_cache["idx"]  # type: ignore[no-any-return]

    # Destination sources (#91): Dan's iOverlander exports and a public-land GeoJSON.
    iover_dir = os.environ.get("IOVERLANDER_DIR", "")
    land_file = os.environ.get("PUBLIC_LAND_FILE", "")

    return TripEdges(
        route=lambda o, d, via, avoid, alt: google_io.compute_route(key, o, d, via, avoid, alt),
        search=lambda seg, tok, q, typ: google_io.search_along(key, seg, tok, q, typ),
        now=lambda: datetime.now(UTC),
        eia=lambda: eia_io.fetch_diesel(eia_key),
        forecasts=weather_io.fetch_forecasts,
        alerts=weather_io.fetch_alerts,
        fuel_state=fuel_state,
        position=position,
        pois=pois,
        drive_s=lambda o, d: google_io.drive_leg(key, o, d),
        ioverlander=(lambda: load_ioverlander(Path(iover_dir))) if iover_dir else None,
        public_land=(lambda: load_public_land(Path(land_file))) if land_file else None,
        campgrounds=lambda bbox: google_io.search_campgrounds(key, bbox),
        byways=byways if byways_file else None,
    )


def _fuel_plan(args: argparse.Namespace) -> int:
    import asyncio
    import json
    from datetime import datetime

    from bigair.fuel_plan import (
        PlanInput,
        PlanRefusedError,
        format_options,
        naive_plan,
        plan_options,
        trip_from_dict,
        trip_to_dict,
    )
    from bigair.fuel_sources import live_fuel_from_state, parse_discounts
    from bigair.trip_service import TripError, TripRequest, plan_trip, weather_summary

    reserves = tuple(float(r) for r in args.reserves.split(","))
    if args.origin and args.to:  # live: route, prices and weather from the network
        try:
            req = TripRequest(
                origin=args.origin,
                destination=args.to,
                via=tuple(args.via or ()),
                round_trip=args.round_trip,
                local_mi=args.local_mi,
                depart=datetime.fromisoformat(args.depart) if args.depart else None,
                return_depart=(
                    datetime.fromisoformat(args.return_depart) if args.return_depart else None
                ),
                reserves=reserves,
                discounts=parse_discounts(args.discount),
                tank_gal=args.tank_gal,
                mpg=args.mpg,
                calibrating=args.calibrating,
                capacity_gal=args.capacity,
                stop_penalty_usd=args.stop_penalty,
                home_price=args.home_price,
                max_age_h=args.max_age_h,
            )
            r = plan_trip(req, _trip_edges(args.mqtt_host, gpsd=False))
        except TripError as e:
            print(f"refused: {e}")  # CLI output, not operational logging
            return 2
        if args.save:
            trip = trip_to_dict(r.trip_mi, r.stations, r.inp.end_value_per_gal)
            Path(args.save).write_text(json.dumps(trip, indent=1))
        lines = [f"Note: {w}" for w in r.warnings]
        if r.unpriced:
            names = ", ".join(
                f"{u.name}{', ' + u.where if u.where else ''} (mi {u.mile:.0f})" for u in r.unpriced
            )
            lines.append(f"Open Roads stations Google has no price for (check the app): {names}")
        lines += [
            f"Alert: {a.event}, {a.area[:70]}"
            + (f" (until {a.ends:%a %H:%M} UTC)" if a.ends else "")
            for a in r.alerts
        ]
        lines += [f"Weather {w}" for w in weather_summary(r)]
        print("\n".join([*lines, "", format_options(r.inp, r.options, r.naive)]))
        return 0

    if not args.trip:
        print("give a trip file, or --from and --to")  # CLI output
        return 2
    route_mi, stations, home = trip_from_dict(json.loads(Path(args.trip).read_text()))
    tank, mpg, calibrating, stale = args.tank_gal, args.mpg, args.calibrating, False
    if args.live:
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
    try:
        text = format_options(inp, plan_options(inp, reserves), naive_plan(inp))
    except PlanRefusedError as e:
        print(f"refused: {e}")  # CLI output, not operational logging
        return 2
    print(text)  # CLI output, not operational logging
    return 0


def _claude_service(edges: TripEdges | None, rules: TravelRules, cache: Path) -> ClaudeService:
    """Claude for the planner (#89): disabled (but answering status) without a key."""
    import asyncio
    from datetime import UTC, datetime

    from bigair import claude_io, weather_io
    from bigair.claude_service import ClaudeService
    from bigair.claude_tools import ToolDeps, forecast_from
    from bigair.gpsd_io import current_fix
    from bigair.poi import load_pois, read_note_body
    from bigair.state_file import StateFileError, read_state

    poi_dir = Path(os.environ.get("POI_DIR", "/poi"))

    def position() -> tuple[float, float] | None:
        if edges is not None and edges.position is not None:
            return edges.position()
        fix = asyncio.run(current_fix())
        return (fix.lat, fix.lon) if fix else None

    def last_plan() -> dict[str, Any] | None:
        try:
            return read_state(cache)
        except StateFileError:
            return None

    deps = ToolDeps(
        position=position,
        pois=lambda: load_pois(poi_dir)[0],
        read_note=lambda name: read_note_body(poi_dir, name),
        last_plan=last_plan,
        forecast=forecast_from(weather_io.fetch_forecasts),
    )
    send = claude_io.make_send(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    cap = float(os.environ.get("CLAUDE_MONTHLY_CAP_USD") or 25.0)
    return ClaudeService(send, deps, rules, cache.parent, cap, lambda: datetime.now(UTC))


def _planner(args: argparse.Namespace) -> int:
    from loguru import logger

    from bigair.planner_server import make_server
    from bigair.poi_near import reach_from_json
    from bigair.travel_rules import TravelRules, load_rules
    from bigair.trip_service import (
        TripError,
        chain_plan,
        chain_request_from_json,
        chain_to_json,
        discover,
        discover_request_from_json,
        discover_to_json,
        plan_trip,
        pois_for_cached,
        request_from_json,
        result_to_json,
    )

    try:
        edges = _trip_edges(args.mqtt_host)
    except TripError as e:
        # Serve the page and the cached plan anyway; planning says what's missing.
        logger.error("planner can't plan: {}", e)
        problem = str(e)
        edges = None

    # Dan's travel rules for slow mode (#90); a bad file is logged, not fatal.
    rules_path = os.environ.get("TRAVEL_RULES", "")
    try:
        rules = load_rules(Path(rules_path) if rules_path else None)
    except ValueError as e:
        logger.error("travel rules ignored, using defaults: {}", e)
        rules = TravelRules()

    def run(payload: dict[str, Any]) -> dict[str, Any]:
        if edges is None:
            raise TripError(f"{problem} on the Pi (deploy/.env)")
        return result_to_json(plan_trip(request_from_json(payload), edges, rules))

    def run_pois(payload: dict[str, Any], last: dict[str, Any]) -> dict[str, Any]:
        if edges is None:
            raise TripError(f"{problem} on the Pi (deploy/.env)")
        reach = payload.get("reach")
        try:
            r = reach_from_json(reach if isinstance(reach, dict) else None)
        except ValueError as e:
            raise TripError(str(e)) from e
        return pois_for_cached(last, r, edges, rules)

    def run_destinations(payload: dict[str, Any]) -> dict[str, Any]:
        if edges is None:
            raise TripError(f"{problem} on the Pi (deploy/.env)")
        return discover_to_json(discover(discover_request_from_json(payload), edges, rules))

    def run_chain(payload: dict[str, Any]) -> dict[str, Any]:
        if edges is None:
            raise TripError(f"{problem} on the Pi (deploy/.env)")
        return chain_to_json(chain_plan(chain_request_from_json(payload), edges, rules))

    claude = _claude_service(edges, rules, Path(args.cache))

    server = make_server(
        args.host,
        args.port,
        run,
        Path(args.cache),
        daily_limit=args.daily_limit,
        run_pois=run_pois,
        run_destinations=run_destinations,
        run_chain=run_chain,
        claude=claude,
    )
    logger.info("trip planner on {}:{}", args.host, args.port)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
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
    fp.add_argument("--return-depart", help="live round trip: ISO time you leave for home")
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
    pl = sub.add_parser("planner", help="serve the trip planner page (#75)")
    pl.add_argument("--host", default="0.0.0.0")  # noqa: S104 — van LAN + tailnet, rate-limited
    pl.add_argument("--port", type=int, default=int(os.environ.get("PLANNER_PORT", "8092")))
    pl.add_argument("--cache", default=os.environ.get("PLANNER_CACHE", "/data/trip-plan.json"))
    pl.add_argument(
        "--daily-limit", type=int, default=int(os.environ.get("PLANNER_DAILY_LIMIT", "20"))
    )
    pl.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
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
    if args.command == "planner":
        return _planner(args)
    return 0
