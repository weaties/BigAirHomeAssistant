"""CLI entry point — wiring only, no business logic."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _display(args: argparse.Namespace) -> int:
    from loguru import logger

    from bigair.display_render import GaugeState
    from bigair.display_server import debug_file_reader, make_server

    # M1 (#17): replace with the fuel model's state from MQTT. Until then the
    # gauge truthfully shows "NO FUEL DATA" (CLAUDE.md: a wrong fuel number is
    # worse than no number).
    def state() -> GaugeState:
        return GaugeState(tank_gal=None, range_mi=None)

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bigair", description="Van telemetry services")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")
    disp = sub.add_parser("display", help="serve the dash fuel gauge frame (eink-screen firmware)")
    disp.add_argument("--host", default="0.0.0.0")  # noqa: S104 — van LAN; Bearer key required
    disp.add_argument("--port", type=int, default=int(os.environ.get("DISPLAY_PORT", "8090")))
    args = parser.parse_args(argv)
    if args.version:
        from importlib.metadata import version

        print(version("bigair"))  # CLI output, not operational logging
        return 0
    if args.command == "display":
        return _display(args)
    return 0
