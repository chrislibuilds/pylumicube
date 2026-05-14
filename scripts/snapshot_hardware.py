"""Snapshot the live cube's discoverable state into a single text file.

Run this on the Pi while the original daemon is stopped but the hardware
is in a known-good state. Captures, for every discovered node:

- node id, preferred name (GET_PREFERRED_NAME)
- full ENUMERATE_FIELDS walk: per-field key, name, type, size, span,
  module string, units string

Intended as a reference snapshot before retiring the current OS image
(see TODO.md). Output goes to stdout; redirect to a file and commit it
under `ref_data/`.

Usage:
    python scripts/snapshot_hardware.py > ref_data/cube_snapshot.txt
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "src")

from pylumicube import LumiCube  # noqa: E402
from pylumicube.constants import TYPE_ENUMERATE_FIELDS  # noqa: E402


# ENUMERATE_FIELDS sub-dictionary keys, mirroring the Java daemon's
# FieldMetadataKey enum (ref_data/lumicube-daemon/.../bus/FieldMetadataKey.java).
META_NAME = 0
META_TYPE = 1
META_SIZE = 2
META_SPAN = 3
META_UNITS = 9
META_MODULE = 12


META_GETTABLE = 4
META_SETTABLE = 5
META_IDEMPOTENT = 6
META_MIN_VALUE = 7
META_MAX_VALUE = 8
META_DEBUG = 10
META_SYSTEM = 11


def _skip_op(value: int) -> bytes:
    """Encode a FlatDictionary skip with the narrowest width that fits.

    The skip cmd byte is `0x10 | width_in_bytes`. Width 0 means "skip 0"
    (i.e., nothing); we still emit a width-1 zero skip to keep the
    response shape predictable when called with value=0.
    """
    if value <= 0xFF:
        return bytes([0x11, value])
    if value <= 0xFFFF:
        return bytes([0x12]) + value.to_bytes(2, "little")
    if value <= 0xFFFFFF:
        return bytes([0x13]) + value.to_bytes(3, "little")
    if value <= 0xFFFFFFFF:
        return bytes([0x14]) + value.to_bytes(4, "little")
    raise ValueError(f"skip value {value} doesn't fit in 4 bytes")


def build_query(k0: int, k1: int = 0) -> bytes:
    """ENUMERATE_FIELDS query: outer skip k0, run-of-1, optional sub-dict skip k1.

    Matches the format `scripts/query_key.py` uses (which is empirically
    known to work against the cube firmware). When k1 == 0 we omit the
    inner skip entirely — the cube interprets the request as "give me the
    whole sub-dict for this field".
    """
    out = bytearray()
    if k0 > 0:
        out += _skip_op(k0)
    out += bytes([0x01, 0x01])
    if k1 > 0:
        out += _skip_op(k1)
    return bytes(out)


def parse_step(data: bytes, offset: int):
    cmd = data[offset]
    width = cmd & 0x0F
    disc = (cmd >> 4) & 0x0F
    param = int.from_bytes(data[offset + 1 : offset + 1 + width], "little") if width else 0
    return disc, param, offset + 1 + width


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    """The cube firmware uses a 1-byte length prefix (CLAUDE.md item 8)."""
    length = data[offset]
    end = offset + 1 + length
    return data[offset + 1 : end].decode("utf-8", errors="replace"), end


def _consume_subkey(data: bytes, offset: int, sub_key: int,
                    field_size: int | None) -> tuple[object, int]:
    """Decode one sub-dict value at the given metadata key.

    Sub-keys come from BootstrapMetadata.java: name(0)/type(1)/size(2)/span(3)/
    gettable(4)/settable(5)/idempotent(6)/min(7)/max(8)/units(9)/debug(10)/
    system(11)/module(12).
    """
    if sub_key in (META_NAME, META_UNITS, META_MODULE):
        return _read_string(data, offset)
    if sub_key == META_SPAN:
        return int.from_bytes(data[offset : offset + 4], "little"), offset + 4
    if sub_key in (META_MIN_VALUE, META_MAX_VALUE):
        # Typed by the field's own type/size, which we read earlier in this
        # same sub-dict. Fall back to 1 byte if we haven't seen size yet.
        width = field_size if field_size and field_size > 0 else 1
        return int.from_bytes(data[offset : offset + width], "little"), offset + width
    # Everything else (type/size/gettable/settable/idempotent/debug/system)
    # is a single byte UINT/BOOLEAN.
    return data[offset], offset + 1


def parse_subdict(data: bytes) -> dict[int, object]:
    """Parse a metadata sub-dictionary that runs to the end of `data`.

    ENUMERATE_FIELDS responses do NOT length-prefix the sub-dict — the
    cube emits it directly after the outer `01 01` run-1 command. Per
    CLAUDE.md item 7, each sub-key is emitted as its own `01 01 <value>`
    (run-1), with `1N <skip>` ops covering the keys the firmware did not
    populate. We walk to the end of the buffer.
    """
    out: dict[int, object] = {}
    keyacc = 0
    offset = 0
    while offset < len(data):
        disc, param, offset = parse_step(data, offset)
        if disc == 1:
            keyacc += param
            continue
        if disc == 2:  # END_DICTIONARY — present in firmware sources but never seen on wire
            break
        if disc != 0:
            raise ValueError(f"unknown sub-dict discriminator {disc} at offset {offset}")
        for _ in range(param):
            seen_size = out.get(META_SIZE)
            field_size = seen_size if isinstance(seen_size, int) else None
            value, offset = _consume_subkey(data, offset, keyacc, field_size)
            out[keyacc] = value
            keyacc += 1
    return out


def parse_response(data: bytes) -> dict[int, dict]:
    """Parse a top-level ENUMERATE_FIELDS response into {relative_key: sub_meta}.

    The outer dict is constrained: optional leading SKIPs, then at most
    one RUN-1 whose value (a sub-dict) consumes the rest of the buffer.
    No length prefix on the sub-dict, so it can only appear at the tail.
    A response with only SKIPs (or empty) means "no field at the queried
    key" and the caller should step forward.
    """
    out: dict[int, dict] = {}
    keyacc = 0
    offset = 0
    while offset < len(data):
        disc, param, offset = parse_step(data, offset)
        if disc == 1:
            keyacc += param
            continue
        if disc == 2:
            break
        if disc != 0:
            raise ValueError(f"unknown outer discriminator {disc} at offset {offset}")
        if param != 1:
            raise ValueError(f"expected outer run-1, got run-{param}")
        out[keyacc] = parse_subdict(data[offset:])
        return out
    return out


def _signature(meta: dict) -> tuple:
    """Stable identity for a field block. Two queries inside the same block
    return identical metadata, so we use (name, type, size, span, module)."""
    return (
        meta.get(META_NAME),
        meta.get(META_TYPE),
        meta.get(META_SIZE),
        meta.get(META_SPAN),
        meta.get(META_MODULE),
    )


def _probe(transport, node_id: int, k0: int) -> tuple[dict, int] | None:
    """One ENUMERATE_FIELDS query. Returns (meta, rel_key) where rel_key is
    the leading-skip value the cube wrote into the response (i.e., the
    relative position of the field within the outer dict). None if the
    response was empty or unparseable.

    The cube uses rel_key two ways:
      - Inside a block: rel_key == k0 (the cube echoes the query position).
      - In a gap: rel_key == (next_block_floor - k0), pointing forward.
    Comparing rel_key to k0 distinguishes the two cases.
    """
    try:
        future = transport.send_request(
            dest_id=node_id, type_id=TYPE_ENUMERATE_FIELDS,
            payload=build_query(k0, 0), timeout=3.0,
        )
        response = future.result(timeout=4.0)
    except Exception:
        return None
    if not response:
        return None
    try:
        parsed = parse_response(response)
    except Exception:
        return None
    if not parsed:
        return None
    rel_key = next(iter(parsed))  # exactly one entry per query (run-1)
    return parsed[rel_key], rel_key


def _classify(transport, node_id: int, k: int, sig: tuple) -> tuple[str, int]:
    """Probe at k and decide whether the cube's response means k is inside
    block `sig`, or in a gap pointing forward to it.

    Returns one of:
      ("nope", _)            — different sig or no response
      ("at_floor", k)        — k IS the block's floor (rel=0 in response)
      ("echo", k)            — k is somewhere inside the block (floor unknown)
      ("gap", floor)         — k is in a gap; the block starts at `floor`

    Handles the rel == k ambiguity by probing k+1 once (cheap), which
    distinguishes the two cases because rel decreases by 1 under gap and
    tracks k under echo.
    """
    result = _probe(transport, node_id, k)
    if result is None:
        return "nope", 0
    meta, rel = result
    if _signature(meta) != sig:
        return "nope", 0
    if rel == 0:
        return "at_floor", k
    if rel != k:
        return "gap", k + rel
    # rel == k: ambiguous. Disambiguate by probing one step forward.
    result2 = _probe(transport, node_id, k + 1)
    if result2 is None:
        # Past the end of the schema. Most likely k was the final-block floor.
        return "at_floor", k
    meta2, rel2 = result2
    if _signature(meta2) != sig:
        # Different block at k+1: k was the last key inside our block. Floor
        # is still <= k, so report "echo" so the caller binary-searches lower.
        return "echo", k
    if rel2 != k + 1:
        # Gap response at k+1 pointing to our block: floor = (k+1) + rel2.
        return "gap", (k + 1) + rel2
    # rel2 == k + 1: echo too. Genuinely inside the block.
    return "echo", k


def _resolve_floor(transport, node_id: int, hit_k: int, hit_rel: int,
                   sig: tuple) -> int:
    """Find the actual floor of the block sig, given a hit at hit_k whose
    response leading-skip was hit_rel. Uses `_classify` repeatedly to skip
    past the cube's gap responses and ambiguities; binary-searches backward
    only when we're confirmed echo (inside the block at unknown floor).
    """
    if hit_k == 0:
        return 0
    if hit_rel == 0:
        return hit_k
    if hit_rel != hit_k:
        return hit_k + hit_rel
    # hit_rel == hit_k: ambiguous at the top level. Disambiguate via _classify.
    kind, val = _classify(transport, node_id, hit_k, sig)
    if kind in ("at_floor", "nope"):
        return val if kind == "at_floor" else hit_k
    if kind == "gap":
        return val
    # kind == "echo": floor is in [max(0, hit_k - span + 1), hit_k]. Binary-search.
    span = sig[3] if isinstance(sig[3], int) and sig[3] > 0 else 1
    lo = max(0, hit_k - span + 1)
    hi = hit_k
    while lo < hi:
        mid = (lo + hi) // 2
        kind, val = _classify(transport, node_id, mid, sig)
        if kind == "nope":
            lo = mid + 1
        elif kind == "at_floor":
            return val
        elif kind == "gap":
            return val
        else:  # "echo": still inside the block at mid, floor ≤ mid
            hi = mid
    return lo


# Keys known to host fields the cube's ENUMERATE_FIELDS walk skips over.
# The auto-walk only follows the cube's "next field at floor ≥ k" hints,
# which on the current firmware jumps straight from speaker.* to system
# fields, missing the display module entirely. These offsets are taken
# from scripts/query_key.py results + metadata.py — extend as new modules
# get discovered.
CUBE_WELL_KNOWN_KEYS = (
    256, 257, 258, 259, 260, 261, 262, 263, 264, 265,  # brightness, panel_*, gamma_*
    266,    # led_colour (span 999)
    1265,   # spread_spectrum_period
    1266,   # show
)


def _print_row(floor: int, meta: dict, span_val) -> None:
    name = str(meta.get(META_NAME, "?"))
    ftype = meta.get(META_TYPE, "?")
    size = meta.get(META_SIZE, "?")
    module = str(meta.get(META_MODULE, ""))
    units = str(meta.get(META_UNITS, ""))
    flags = "".join([
        "g" if meta.get(META_GETTABLE) else "-",
        "s" if meta.get(META_SETTABLE) else "-",
        "i" if meta.get(META_IDEMPOTENT) else "-",
        "d" if meta.get(META_DEBUG) else "-",
        "S" if meta.get(META_SYSTEM) else "-",
    ])
    print(f"  {floor:>6} | {name:28s} | {ftype!s:>4} | {size!s:>4} | "
          f"{span_val!s:>6} | {module:18s} | {units:10s} | {flags}")


def _print_header() -> None:
    print(f"  {'KEY':>6} | {'NAME':28s} | {'TYPE':>4} | {'SIZE':>4} | "
          f"{'SPAN':>6} | {'MODULE':18s} | {'UNITS':10s} | FLAGS")
    print(f"  {'-' * 6} | {'-' * 28} | {'-' * 4} | {'-' * 4} | "
          f"{'-' * 6} | {'-' * 18} | {'-' * 10} | -----")


def dump_node_fields(cube: LumiCube, node_id: int, *,
                     max_iters: int = 400, max_empty: int = 4,
                     extra_probe_keys: tuple[int, ...] = ()) -> int:
    """Walk a node's ENUMERATE_FIELDS schema, printing one line per field.

    Iteration model: query at abs_key. The cube echoes abs_key as the
    leading skip in the response (it doesn't directly tell us a block's
    floor), so for each hit we use `_resolve_floor` (which probes / binary
    searches as needed) to find the real floor, then advance to
    floor + span. Empty responses past the last field accumulate a streak
    counter; we bail after `max_empty` consecutive empties.

    After the auto-walk, `extra_probe_keys` (e.g. CUBE_WELL_KNOWN_KEYS for
    the display module) are probed explicitly to catch blocks the walk
    can't reach.
    """
    transport = cube.transport
    _print_header()
    abs_key = 0
    count = 0
    empty_streak = 0
    seen_sigs: set[tuple] = set()
    last_msg = None
    for _ in range(max_iters):
        result = _probe(transport, node_id, abs_key)
        if result is None:
            empty_streak += 1
            if empty_streak >= max_empty:
                break
            abs_key += 1
            continue
        empty_streak = 0
        meta, rel = result
        sig = _signature(meta)
        if sig in seen_sigs:
            # Same block as a previously reported field — we landed inside it
            # again somehow. Step forward and keep searching.
            abs_key += 1
            continue
        seen_sigs.add(sig)
        floor = _resolve_floor(transport, node_id, abs_key, rel, sig)
        span = sig[3] if isinstance(sig[3], int) else 0
        _print_row(floor, meta, span)
        count += 1
        abs_key = floor + max(1, span)
    else:
        last_msg = f"(stopped after {max_iters} iterations; next k0={abs_key})"
    if last_msg:
        print(f"  {last_msg}")

    # Explicit probe pass for keys the walk doesn't reach (e.g. display fields
    # the cube hides from "next field ≥ k" traversal). Print every probed key
    # so overlap cases are visible — the cube has been observed to return
    # different blocks at the same key depending on what's been queried before.
    if extra_probe_keys:
        print()
        print(f"  -- direct probes at well-known keys:")
        for key in extra_probe_keys:
            result = _probe(transport, node_id, key)
            if result is None:
                print(f"  probe @{key:>6}: (no response)")
                continue
            meta, rel = result
            sig = _signature(meta)
            floor = _resolve_floor(transport, node_id, key, rel, sig)
            name = str(meta.get(META_NAME, "?"))
            module = str(meta.get(META_MODULE, ""))
            span_val = sig[3]
            if sig in seen_sigs:
                marker = "dup"
            else:
                seen_sigs.add(sig)
                count += 1
                marker = "NEW"
            print(f"  probe @{key:>6} -> floor={floor:>6}  span={span_val!s:>4}  "
                  f"{name:24s}  module={module:14s}  [{marker}]")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyAMA0")
    parser.add_argument("--handshake-timeout", type=float, default=10.0)
    parser.add_argument("--max-iters", type=int, default=400,
                        help="max ENUMERATE_FIELDS round-trips per node")
    args = parser.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"# lumicube hardware snapshot")
    print(f"# captured: {stamp}")
    print(f"# port:     {args.port}")
    print()

    with LumiCube(args.port, handshake_timeout=args.handshake_timeout) as cube:
        # The LumiCube.start() path already runs allocator + passive discovery
        # + GET_PREFERRED_NAME for everything it sees.
        allocated = dict(cube._allocated)  # noqa: SLF001 — script-only access
        names = dict(cube._names)          # noqa: SLF001
        time.sleep(0.5)  # let any in-flight broadcasts settle

        print(f"## Discovered nodes ({len(allocated)})")
        for node_id, identity in sorted(allocated.items()):
            origin = "allocated" if identity else "passive"
            uid = str(identity) if identity else "-"
            name = names.get(node_id, "<unknown>")
            print(f"  node {node_id:>3}  name={name!r:30s} origin={origin}  uuid={uid}")
        print()

        for node_id in sorted(allocated):
            name = names.get(node_id, "<unknown>")
            extra = CUBE_WELL_KNOWN_KEYS if "cube" in name.lower() else ()
            print(f"## Node {node_id} ({name!r}) — ENUMERATE_FIELDS")
            n = dump_node_fields(cube, node_id, max_iters=args.max_iters,
                                 extra_probe_keys=extra)
            print(f"  ({n} field entries)")
            print()

    print("# end of snapshot")
    return 0


if __name__ == "__main__":
    sys.exit(main())