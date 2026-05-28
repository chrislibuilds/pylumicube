"""Probe the cube's field metadata to locate the LED color field offset."""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from pylumicube.constants import DAEMON_NODE_ID, TYPE_ENUMERATE_FIELDS  # noqa: E402
from pylumicube.link import SerialLink  # noqa: E402
from pylumicube.transport import Transport  # noqa: E402


def build_query(k0: int, k1: int) -> bytes:
    """Skip-query: outer skip k0, run 1, inner skip k1."""
    out = bytearray()
    if k0 > 0xFF:
        out.append(0x12)
        out.extend(k0.to_bytes(2, "little"))
    elif k0 > 0:
        out.append(0x11)
        out.append(k0)
    out.append(0x01)
    out.append(0x01)
    if k1 > 0xFF:
        out.append(0x12)
        out.extend(k1.to_bytes(2, "little"))
    elif k1 > 0:
        out.append(0x11)
        out.append(k1)
    return bytes(out)


def parse_run_cmd(data: bytes, offset: int):
    cmd = data[offset]
    width = cmd & 0x0F
    disc = (cmd >> 4) & 0x0F
    param = int.from_bytes(data[offset + 1 : offset + 1 + width], "little") if width else 0
    return disc, param, offset + 1 + width


def main() -> int:
    link = SerialLink("/dev/ttyAMA0")
    link.start(handshake_timeout=10.0)
    transport = Transport(link, source_id=DAEMON_NODE_ID)
    time.sleep(0.5)

    # Walk by querying with k0 stepping. Read response, look for the floor key
    # of the first returned field (encoded as an outer skip), then step past it.
    floor = 0
    print(f"{'FLOOR':>6}  metadata bytes (first ~40)  ->  decoded name")
    for _ in range(40):
        query = build_query(floor, 0)
        try:
            fut = transport.send_request(dest_id=124, type_id=TYPE_ENUMERATE_FIELDS,
                                         payload=query, timeout=3.0)
            resp = fut.result(timeout=4.0)
        except Exception as exc:
            print(f"floor={floor}: query failed: {exc}")
            break
        if not resp:
            print(f"floor={floor}: empty response, end of fields")
            break

        # Outer dict: maybe a skip then a run-of-1 then sub-dict.
        offset = 0
        skip_amount = 0
        while True:
            disc, param, new_off = parse_run_cmd(resp, offset)
            if disc == 1:
                skip_amount += param
                offset = new_off
            else:
                break
        actual_floor = floor + skip_amount

        # disc should be 0 (run); param is run length (expect 1)
        if disc != 0 or param < 1:
            print(f"floor={floor}: unexpected response shape: {resp.hex()}")
            break
        offset = new_off

        # Sub-dict: parse first few entries to get the name (key 0 = UTF8_STRING)
        sub_offset = offset
        # Look for the name: skip any leading skips, then run-of-N, then varint-length name
        while sub_offset < len(resp):
            d2, p2, no2 = parse_run_cmd(resp, sub_offset)
            sub_offset = no2
            if d2 == 1:
                # skip past the name field (sub-key 0); name is at index 0 so any skip means name is missing
                continue
            # run; first value is the name (UTF8_STRING, varint length-prefixed)
            # Parse varint
            length = 0
            shift = 0
            while True:
                b = resp[sub_offset]
                sub_offset += 1
                length |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            name = resp[sub_offset : sub_offset + length].decode("utf-8", errors="replace")
            sub_offset += length
            break
        else:
            name = "<no name>"

        print(f"{actual_floor:>6}  {resp[:40].hex():80s}  ->  {name!r}")

        # Advance: try +1; if the field has a known span we'd need to skip that many
        # but we don't easily parse it here. Use a heuristic step.
        floor = actual_floor + 1

    link.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
