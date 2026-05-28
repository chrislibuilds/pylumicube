"""`lumicube-run` — exec a community script in the compat namespace.

Usage:
    lumicube-run [--port PATH] [--debug] [--no-clear] <script.py>

The Java `foundry-daemon` must not be running — it holds /dev/ttyAMA0
exclusively. See README.md for the daemon-stop snippet.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..constants import SERIAL_DEVICE
from ..node import LumiCube
from .runtime import _set_hosted_cube, build_globals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lumicube-run",
        description="Run a LumiCube community script against pylumicube.",
    )
    parser.add_argument("script", help="path to the .py script to execute")
    parser.add_argument("--port", default=SERIAL_DEVICE,
                        help=f"serial device (default {SERIAL_DEVICE})")
    parser.add_argument("--debug", action="store_true",
                        help="verbose logging from the link/transport layers")
    parser.add_argument("--handshake-timeout", type=float, default=5.0)
    parser.add_argument("--discovery-timeout", type=float, default=3.0)
    parser.add_argument(
        "--no-clear", action="store_true",
        help="don't blank the LED matrix on exit",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        with open(args.script, "rb") as fh:
            source = fh.read()
    except OSError as exc:
        print(f"error: cannot read script: {exc}", file=sys.stderr)
        return 1

    code = compile(source, args.script, "exec")

    try:
        with LumiCube(
            port=args.port,
            handshake_timeout=args.handshake_timeout,
            discovery_timeout=args.discovery_timeout,
        ) as cube:
            ns = build_globals(cube)
            ns["__file__"] = args.script

            import os
            script_dir = os.path.dirname(os.path.abspath(args.script)) or "."
            if script_dir not in sys.path:
                sys.path.insert(0, script_dir)

            # Register the open cube so native-API scripts can pick it up
            # via `pylumicube.compat.open_or_use_hosted()` instead of
            # opening a second serial connection (which would fail with
            # "Resource busy").
            _set_hosted_cube(cube)
            try:
                exec(code, ns)
            except KeyboardInterrupt:
                # Most community scripts are infinite loops — Ctrl-C is the
                # intended exit. Don't treat it as a failure.
                print("\ninterrupted", file=sys.stderr)
            finally:
                _set_hosted_cube(None)
                if not args.no_clear:
                    try:
                        cube.display.fill(0x000000)
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
