"""Query a single field's metadata by absolute key.

Usage: python scripts/query_key.py <key>
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from pylumicube.constants import DAEMON_NODE_ID, TYPE_ENUMERATE_FIELDS  # noqa: E402
from pylumicube.link import SerialLink  # noqa: E402
from pylumicube.transport import Transport  # noqa: E402


def build_query(k0: int) -> bytes:
    out = bytearray()
    if k0 > 0xFF:
        out.append(0x12)
        out.extend(k0.to_bytes(2, "little"))
    elif k0 > 0:
        out.append(0x11)
        out.append(k0)
    out.append(0x01)
    out.append(0x01)
    return bytes(out)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: query_key.py <key1> [<key2> ...]", file=sys.stderr)
        return 1
    keys = [int(x) for x in sys.argv[1:]]

    link = SerialLink("/dev/ttyAMA0")
    link.start(handshake_timeout=10.0)
    transport = Transport(link, source_id=DAEMON_NODE_ID)
    time.sleep(0.5)

    for k in keys:
        query = build_query(k)
        try:
            fut = transport.send_request(dest_id=124, type_id=TYPE_ENUMERATE_FIELDS,
                                         payload=query, timeout=3.0)
            resp = fut.result(timeout=4.0)
        except Exception as exc:
            print(f"k={k}: query failed: {exc}")
            continue
        print(f"k={k:>5}: {resp.hex()}")
        # Try to find the name in the response (search for varint <= 32 followed by ASCII)
        for i in range(len(resp) - 1):
            l = resp[i]
            if 1 <= l <= 32 and i + 1 + l <= len(resp):
                cand = resp[i + 1 : i + 1 + l]
                if all(0x20 <= c < 0x7F for c in cand):
                    name = cand.decode("ascii")
                    print(f"  ↳ likely name: {name!r}")
                    break

    link.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
