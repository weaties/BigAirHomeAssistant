"""CLI entry point — wiring only, no business logic."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bigair", description="Van telemetry services")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)
    if args.version:
        from importlib.metadata import version

        print(version("bigair"))  # CLI output, not operational logging
    return 0
