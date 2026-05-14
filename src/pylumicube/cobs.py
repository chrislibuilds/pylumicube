"""COBS byte-stuffing.

Direct port of the Java daemon's incremental COBS implementation
(see ref_data/lumicube-daemon/.../common/COBS.java). The Java code
uses an in-place form where index 0 is reserved for the overhead byte
and the encoder rewrites that byte after scanning the data range.

These functions match that semantics. Buffers are constrained to
<= 256 bytes total (PROTOCOL §2.1).
"""


def encode(buffer: bytearray, pointer: int, offset: int, length: int) -> int:
    """Encode `buffer[offset:offset+length]` in place.

    `pointer` is the index where the leading overhead byte will be written
    (typically `offset - 1`). Returns the index of the *last* code byte
    written. Java: COBS.encode().
    """
    if len(buffer) > 256:
        raise ValueError("buffer too large")
    if pointer < 0 or offset < 0 or length < 0 or offset + length > len(buffer):
        raise ValueError("invalid range")

    code = (offset - pointer) & 0xFF  # initial distance-to-next-zero
    for index in range(offset, offset + length):
        value = buffer[index]
        if value == 0:
            buffer[pointer] = code
            pointer = index
            code = 1
        else:
            if code == 0xFF:
                raise ValueError("COBS overflow (run > 254 nonzero bytes)")
            code += 1
    buffer[pointer] = code
    return pointer


def decode(buffer: bytearray, offset: int, length: int) -> None:
    """Decode `buffer[offset:offset+length]` in place.

    On entry `buffer[offset]` is the leading overhead byte and the rest of
    the slice contains COBS-encoded data (no NUL terminator). On exit, the
    overhead bytes have been replaced with 0x00 so the slice is the original
    bytes. Java: COBS.decode().

    Raises ValueError on malformed input.
    """
    if len(buffer) > 256:
        raise ValueError("buffer too large")
    if offset < 0 or length < 0 or offset + length > len(buffer):
        raise ValueError("invalid range")

    counter = 0
    for index in range(offset, offset + length):
        value = buffer[index]
        if value == 0:
            raise ValueError("unexpected NUL byte inside COBS region")
        if counter == 0:
            buffer[index] = 0
            counter = value
        counter -= 1
    if counter != 0:
        raise ValueError("COBS underflow (truncated frame)")
