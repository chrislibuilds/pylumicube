"""Dump the cube node's field metadata via ENUMERATE_FIELDS.

This walks the cube's field schema one entry at a time, printing the
name, key, type, and size of each field. Useful for discovering the
actual key offsets on a multi-module node like 'cube'.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from pylumicube import flat_dictionary, framing, link as link_module, uavcan as u  # noqa: E402
from pylumicube.constants import (  # noqa: E402
    DAEMON_NODE_ID,
    DEFAULT_PRIORITY,
    TYPE_ENUMERATE_FIELDS,
    TYPE_GET_PREFERRED_NAME,
)
from pylumicube.link import SerialLink  # noqa: E402
from pylumicube.transport import Transport  # noqa: E402


def build_query(k0: int, k1: int) -> bytes:
    """Build an ENUMERATE_FIELDS query that skips to k0, then has a 1-element
    run containing a sub-dictionary that skips to k1."""
    # Outer skip to k0 (use 2-byte width to be safe)
    out = bytearray()
    out.append(0x12)  # skip width 2
    out.extend(k0.to_bytes(2, "little"))
    out.append(0x01)  # run width 1
    out.append(0x01)  # length 1
    # Inside the value (sub-dictionary): skip to k1
    out.append(0x12)
    out.extend(k1.to_bytes(2, "little"))
    return bytes(out)


def parse_dict_step(data: bytes, offset: int = 0):
    """Decode a single FlatDictionary command. Returns (discriminator, parameter, new_offset)."""
    cmd = data[offset]
    width = cmd & 0x0F
    disc = (cmd >> 4) & 0x0F
    param = int.from_bytes(data[offset + 1 : offset + 1 + width], "little")
    return disc, param, offset + 1 + width


def parse_response(data: bytes) -> dict:
    """Parse a recursive dictionary response from ENUMERATE_FIELDS."""
    out = {}
    keyacc = 0
    offset = 0
    while offset < len(data):
        disc, param, offset = parse_dict_step(data, offset)
        if disc == 1:  # skip
            keyacc += param
        elif disc == 0:  # run
            for _ in range(param):
                # Each value is a sub-dictionary (with leading length-prefix-as-skip-or-?)
                # Actually inside a run of dictionaries, each value is the size+payload of a sub-dict.
                # Read varint size:
                size = 0
                shift = 0
                while True:
                    b = data[offset]
                    offset += 1
                    size |= (b & 0x7F) << shift
                    if (b & 0x80) == 0:
                        break
                    shift += 7
                sub = data[offset : offset + size]
                offset += size
                out[keyacc] = parse_subdict(sub)
                keyacc += 1
        else:
            raise ValueError(f"unknown discriminator {disc}")
    return out


def parse_subdict(data: bytes) -> dict:
    """Parse a metadata sub-dictionary into {key: raw_bytes}."""
    out = {}
    keyacc = 0
    offset = 0
    while offset < len(data):
        disc, param, offset = parse_dict_step(data, offset)
        if disc == 1:
            keyacc += param
        elif disc == 0:
            for _ in range(param):
                # Inside a sub-dict, value is raw bytes — but their type/size depends on
                # bootstrap metadata which we don't fully implement. For names (key 0) and
                # strings, varint length-prefixed. For UINT/BOOLEAN, 1 byte.
                # Heuristic: treat key 0 (name) as varint-length string, and others as 1-byte UINT.
                if keyacc == 0:
                    size = 0
                    shift = 0
                    while True:
                        b = data[offset]
                        offset += 1
                        size |= (b & 0x7F) << shift
                        if (b & 0x80) == 0:
                            break
                        shift += 7
                    out[keyacc] = data[offset : offset + size].decode("utf-8", errors="replace")
                    offset += size
                elif keyacc == 9 or keyacc == 12:  # units, module — also strings
                    size = 0
                    shift = 0
                    while True:
                        b = data[offset]
                        offset += 1
                        size |= (b & 0x7F) << shift
                        if (b & 0x80) == 0:
                            break
                        shift += 7
                    out[keyacc] = data[offset : offset + size].decode("utf-8", errors="replace")
                    offset += size
                elif keyacc in (3,):  # span (4 bytes)
                    out[keyacc] = int.from_bytes(data[offset : offset + 4], "little")
                    offset += 4
                else:
                    out[keyacc] = data[offset]
                    offset += 1
                keyacc += 1
        else:
            raise ValueError(f"unknown discriminator {disc} in sub-dict")
    return out


def main() -> int:
    port = "/dev/ttyAMA0"
    dest = 124

    link = SerialLink(port)
    link.start(handshake_timeout=10.0)
    transport = Transport(link, source_id=DAEMON_NODE_ID)
    time.sleep(0.5)

    print(f"\nQuerying ENUMERATE_FIELDS on node {dest}...\n")
    print(f"{'KEY':>5} | {'NAME':24s} | {'TYPE':4s} | {'SIZE':4s} | {'SPAN':5s} | {'MODULE':20s}")
    print("-" * 80)

    k0, k1 = 0, 0
    for _ in range(60):
        query = build_query(k0, k1)
        try:
            future = transport.send_request(dest_id=dest, type_id=TYPE_ENUMERATE_FIELDS,
                                            payload=query, timeout=3.0)
            response = future.result(timeout=4.0)
        except Exception as exc:
            print(f"query failed at k0={k0}: {exc}")
            break
        if not response:
            break
        try:
            parsed = parse_response(response)
        except Exception as exc:
            print(f"parse failed at k0={k0}: {exc}; raw: {response.hex()}")
            break
        if not parsed:
            break
        # parsed is {field_key: {meta_key: value}}
        for fk, meta in parsed.items():
            name = meta.get(0, "?")
            ftype = meta.get(1, "?")
            size = meta.get(2, "?")
            span = meta.get(3, "?")
            module = meta.get(12, "")
            print(f"{fk:>5} | {name:24s} | {ftype!s:4s} | {size!s:4s} | {span!s:5s} | {module!s:20s}")
            last_meta = meta
            last_fk = fk
        # Advance to the next field. last_fk is the floor of last entry; subdict has its own keys 0..N
        # Use last_fk + 1 as next k0; k1 stays 0 (each new outer dict starts fresh).
        k0 = last_fk + 1
        k1 = 0

    link.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
