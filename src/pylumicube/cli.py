"""Command-line LED updater.

Usage:
    python -m pylumicube.cli [--port PATH] [--debug] all <hex>
    python -m pylumicube.cli [--port PATH] [--debug] single <index> <hex>
    python -m pylumicube.cli [--port PATH] [--debug] off
"""

from __future__ import annotations

import argparse
import logging
import sys

from .constants import SERIAL_DEVICE
from .node import LumiCube


def _parse_colour(text: str) -> int:
    text = text.lstrip("#")
    if len(text) != 6:
        raise argparse.ArgumentTypeError("colour must be 6 hex digits, e.g. FF0000")
    return int(text, 16) & 0xFFFFFF


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LumiCube LED updater")
    parser.add_argument("--port", default=SERIAL_DEVICE, help=f"serial device (default {SERIAL_DEVICE})")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--handshake-timeout", type=float, default=5.0)
    parser.add_argument("--discovery-timeout", type=float, default=3.0)

    sub = parser.add_subparsers(dest="command", required=True)

    p_all = sub.add_parser("all", help="set every LED to one colour")
    p_all.add_argument("colour", type=_parse_colour, help="6-hex RGB, e.g. FF0000")

    p_single = sub.add_parser("single", help="set one LED")
    p_single.add_argument("index", type=int, help="LED index 0..191")
    p_single.add_argument("colour", type=_parse_colour, help="6-hex RGB")

    sub.add_parser("off", help="turn all LEDs off")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        with LumiCube(
            port=args.port,
            handshake_timeout=args.handshake_timeout,
            discovery_timeout=args.discovery_timeout,
        ) as cube:
            if args.command == "all":
                cube.display.fill(args.colour)
            elif args.command == "single":
                cube.display.set_leds({args.index: args.colour})
            elif args.command == "off":
                cube.display.fill(0x000000)
            else:
                parser.error(f"unknown command {args.command}")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
