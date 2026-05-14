"""One-shot diagnostic: bring up the link, dump every frame, then send
a single SET_FIELDS to the chosen destination node.

Usage:
    python scripts/probe_set_fields.py --port /dev/ttyAMA0 [--dest 124]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

# Make the package importable when run from the repo root
sys.path.insert(0, "src")

from pylumicube import framing  # noqa: E402
from pylumicube.constants import (  # noqa: E402
    CMD_ACKNOWLEDGE,
    CMD_INITIALISE,
    CMD_INITIALISED,
    CMD_MESSAGE,
    CMD_PING,
    CMD_PONG,
    CMD_UNINITIALISED,
    DAEMON_NODE_ID,
    DEFAULT_PRIORITY,
    TYPE_SET_FIELDS,
)
from pylumicube import flat_dictionary, link as link_module, uavcan as u  # noqa: E402
from pylumicube.metadata import display_specs  # noqa: E402

CMD_NAMES = {
    CMD_PING: "PING",
    CMD_PONG: "PONG",
    CMD_INITIALISE: "INIT",
    CMD_INITIALISED: "INITED",
    CMD_UNINITIALISED: "UNINIT",
    CMD_MESSAGE: "MSG",
    CMD_ACKNOWLEDGE: "ACK",
}


def log_frame(direction: str, frame: bytes) -> None:
    stream = framing.FrameStream()
    stream.feed(frame)
    decoded = stream.pop()
    if decoded is None:
        print(f"{direction} <bad frame>: {frame.hex()}")
        return
    name = CMD_NAMES.get(decoded.command, f"0x{decoded.command:02x}")
    print(f"{direction} {name:6s} body={decoded.body.hex()}  raw={frame.hex()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyAMA0")
    parser.add_argument("--dest", type=int, default=124)
    parser.add_argument("--src", type=int, default=DAEMON_NODE_ID)
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Monkey-patch the link's _send_raw to log every TX
    original_send = link_module.SerialLink._send_raw

    def logging_send(self, data: bytes) -> None:
        log_frame("TX", data)
        original_send(self, data)

    link_module.SerialLink._send_raw = logging_send  # type: ignore

    # Patch _dispatch to log every RX
    original_dispatch = link_module.SerialLink._dispatch

    def logging_dispatch(self, frame: framing.DecodedFrame) -> None:
        name = CMD_NAMES.get(frame.command, f"0x{frame.command:02x}")
        print(f"RX {name:6s} body={frame.body.hex()}")
        original_dispatch(self, frame)

    link_module.SerialLink._dispatch = logging_dispatch  # type: ignore

    link = link_module.SerialLink(args.port)
    print("starting link, waiting for handshake...")
    link.start(handshake_timeout=10.0)
    print("handshake complete; waiting 1s for activity...")
    time.sleep(1.0)

    # Build a minimal SET_FIELDS payload: single LED 0 = red + show
    # Match the actual first batch of `fill`: 80 LEDs starting at key 266.
    leds = {266 + i: 0xFF0000 for i in range(80)}
    payload = flat_dictionary.serialise(leds, display_specs())
    print(f"\nSET_FIELDS payload ({len(payload)} bytes): {payload.hex()}")

    message_id = u.encode_request(args.src, args.dest, TYPE_SET_FIELDS, DEFAULT_PRIORITY)
    body = u.pack_message(transfer_id=0, message_id=message_id, payload=payload)
    print(f"UAVCAN body ({len(body)} bytes): {body.hex()}")
    print(f"Submitting MESSAGE...\n")

    fut = link.submit(body)
    try:
        fut.result(timeout=5.0)
        print("MESSAGE was ACKed by cube (link-layer success)")
    except Exception as exc:
        print(f"MESSAGE was NOT ACKed: {exc}")

    print("\nWaiting 2s for service response...")
    time.sleep(2.0)
    link.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
