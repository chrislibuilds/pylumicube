"""FlatDictionary wire encoding (PROTOCOL.md §4.8).

Stream of TLV commands that walk a virtual key cursor (`keyAccumulator`)
and emit values along the way. Each command is one byte:

    bit 7 6 5 4 3 2 1 0
        d d d d w w w w
    d = discriminator (4 bits)
    w = parameter width in bytes (4 bits, 0..4)

Discriminators: 0 = run, 1 = skip. The next `w` bytes are an unsigned LE
integer parameter. Values inside a run are encoded according to the
metadata at the current key.

Reference: ref_data/lumicube-daemon/.../bus/FlatDictionary.java
(line 38: `var command = (byte) (0x10 | width);`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .metadata import FieldSpec


def _width_bytes(value: int) -> int:
    """Number of bytes needed to LE-encode an unsigned int (1..4). Mirrors
    the Java `4 - (Integer.numberOfLeadingZeros(v) / 8)` idiom."""
    if value == 0:
        return 1  # the only legitimate 0 is the leading skip; we still need a width to encode it
    if value < 0:
        raise ValueError("negative parameter")
    if value < 1 << 8:
        return 1
    if value < 1 << 16:
        return 2
    if value < 1 << 24:
        return 3
    if value < 1 << 32:
        return 4
    raise ValueError("parameter exceeds 32 bits")


def _emit_command(buf: bytearray, discriminator: int, parameter: int) -> None:
    width = _width_bytes(parameter)
    buf.append(((discriminator & 0x0F) << 4) | width)
    buf.extend(parameter.to_bytes(width, "little"))


def serialise(values: Mapping[int, int | bytes], specs: Mapping[int, FieldSpec]) -> bytes:
    """Encode a sparse `{key: value}` map as a FlatDictionary.

    `specs` provides the FieldSpec for each *floor* key. Keys in `values`
    must lie within `[floor, floor+span)` of some spec, and all keys in a
    contiguous run must share the same floor (homogeneous block).

    Integer values are LE-encoded at the spec's `size`. Bytes values are
    written verbatim (used for variable-width like the 3-byte LED RGB
    triple — pass an int and we'll encode it).
    """
    if not values:
        return b""
    sorted_keys = sorted(values.keys())
    floor_lookup = _FloorIndex(specs)
    buf = bytearray()
    key_acc = 0
    pos = 0
    while pos < len(sorted_keys):
        # delimit a run of contiguous keys
        run_start = pos
        run_end = pos
        while run_end + 1 < len(sorted_keys) and sorted_keys[run_end + 1] == sorted_keys[run_end] + 1:
            run_end += 1

        # skip command (legitimate 0 only at the very start)
        skip_value = sorted_keys[run_start] - key_acc
        if skip_value < 0:
            raise ValueError("unordered keys")
        if skip_value > 0 or pos == 0 and skip_value == 0 and len(values) > 0 and sorted_keys[0] == 0:
            # Java emits a skip only when skip_value > 0 (see line 36-43); we mirror that.
            if skip_value > 0:
                _emit_command(buf, discriminator=1, parameter=skip_value)
        key_acc += skip_value

        # run command
        run_length = run_end - run_start + 1
        _emit_command(buf, discriminator=0, parameter=run_length)

        # serialise values one homogeneous block at a time
        run_remaining = run_length
        cursor = run_start
        while run_remaining > 0:
            first_key = sorted_keys[cursor]
            spec = floor_lookup.lookup(first_key)
            block_len = min(run_remaining, spec.floor + spec.span - first_key)
            for i in range(block_len):
                key = sorted_keys[cursor + i]
                value = values[key]
                buf.extend(_encode_value(value, spec))
            run_remaining -= block_len
            cursor += block_len
            key_acc += block_len
        pos = run_end + 1
    return bytes(buf)


class SizeTracker:
    """Incrementally track the encoded size of a FlatDictionary.

    Mirrors the byte-counting half of `serialise` so that batching code
    can decide where to split a large `{key: value}` payload without
    re-serialising on every candidate key (which was O(n²)).

    Keys must be `commit`'d in strictly ascending order. The size
    reported includes skip + run commands and value bytes, and matches
    `len(serialise({k0, k1, ...}, specs))` exactly.

    Two-step API:

      cost = tracker.cost_delta(key, spec)
      if tracker.size + cost > BUDGET:
          flush current batch, start a new tracker
      else:
          tracker.commit(key, spec)
    """

    __slots__ = ("_size", "_key_acc", "_last_key", "_run_length")

    def __init__(self) -> None:
        self._size = 0
        self._key_acc = 0
        self._last_key: int | None = None
        self._run_length = 0

    @property
    def size(self) -> int:
        return self._size

    def cost_delta(self, key: int, spec: FieldSpec) -> int:
        """How many bytes adding `key` (with FieldSpec `spec`) would add."""
        delta = 0
        starts_new_run = self._last_key is None or key != self._last_key + 1
        if starts_new_run:
            skip_value = key - self._key_acc
            if skip_value > 0:
                delta += 1 + _width_bytes(skip_value)   # skip cmd + param
            delta += 1 + 1                              # run cmd + 1-byte length param
        else:
            # Extending the current run: run-length parameter width might widen.
            new_run_len = self._run_length + 1
            new_w = _width_bytes(new_run_len)
            old_w = _width_bytes(self._run_length)
            if new_w != old_w:
                delta += new_w - old_w
        delta += spec.size                              # value bytes
        return delta

    def commit(self, key: int, spec: FieldSpec) -> int:
        """Add `key` to the tracker state. Returns the new total size."""
        if self._last_key is None or key != self._last_key + 1:
            skip_value = key - self._key_acc
            if skip_value > 0:
                self._size += 1 + _width_bytes(skip_value)
            self._size += 1 + 1
            self._run_length = 1
        else:
            new_run_len = self._run_length + 1
            new_w = _width_bytes(new_run_len)
            old_w = _width_bytes(self._run_length)
            if new_w != old_w:
                self._size += new_w - old_w
            self._run_length = new_run_len
        self._size += spec.size
        self._last_key = key
        self._key_acc = key + 1
        return self._size


def _encode_value(value: int | bytes, spec: FieldSpec) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        if spec.size and len(value) != spec.size:
            raise ValueError(f"raw value size mismatch: got {len(value)}, expected {spec.size}")
        return bytes(value)
    if not isinstance(value, int):
        raise TypeError("value must be int or bytes")
    size = spec.size
    if size <= 0:
        raise ValueError("variable-width fields not supported by this encoder yet")
    return int(value).to_bytes(size, "little", signed=spec.signed)


class _FloorIndex:
    """Helper to map a key to its containing FieldSpec."""

    def __init__(self, specs: Mapping[int, FieldSpec]) -> None:
        # We index by floor; given a key, find the largest floor <= key.
        self._floors = sorted(specs.keys())
        self._specs = specs

    def lookup(self, key: int) -> FieldSpec:
        # Linear scan is fine for small spec sets (the display has ~12 entries).
        floor = None
        for candidate in self._floors:
            if candidate <= key:
                floor = candidate
            else:
                break
        if floor is None:
            raise KeyError(f"no metadata covers key {key}")
        spec = self._specs[floor]
        if key >= spec.floor + spec.span:
            raise KeyError(f"key {key} out of span at floor {floor}")
        return spec


# --------------------- minimal decoder ---------------------

def deserialise(data: bytes, specs: Mapping[int, FieldSpec]) -> dict[int, int]:
    """Decode a FlatDictionary into a `{key: value}` map. Only fixed-size
    integral fields are supported (sufficient for PUBLISHED_FIELDS telemetry
    on the modules we exercise)."""
    floor_lookup = _FloorIndex(specs)
    out: dict[int, int] = {}
    key_acc = 0
    offset = 0
    while offset < len(data):
        cmd = data[offset]
        offset += 1
        width = cmd & 0x0F
        discriminator = (cmd >> 4) & 0x0F
        if width > 4:
            raise ValueError("invalid width")
        parameter = int.from_bytes(data[offset : offset + width], "little") if width else 0
        offset += width
        if discriminator == 1:  # skip
            key_acc += parameter
            continue
        if discriminator != 0:
            raise ValueError(f"unknown discriminator {discriminator}")
        run_remaining = parameter
        while run_remaining > 0:
            spec = floor_lookup.lookup(key_acc)
            block_len = min(run_remaining, spec.floor + spec.span - key_acc)
            for i in range(block_len):
                size = spec.size
                if size <= 0:
                    raise ValueError("variable-width decode unsupported")
                raw = int.from_bytes(data[offset : offset + size], "little", signed=spec.signed)
                out[key_acc + i] = raw
                offset += size
            key_acc += block_len
            run_remaining -= block_len
    return out
