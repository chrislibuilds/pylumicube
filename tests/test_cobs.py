"""COBS encoder/decoder — verifies in-place semantics and round-trip.

Uses test vectors from the canonical COBS paper (Cheshire & Baker 1999)
plus a couple of LumiCube-specific cases derived from the captured strace.
"""

from pylumicube import cobs


def _round_trip(decoded: bytes) -> bytes:
    """Encode `decoded` (the post-COBS bytes) into a wire frame and return it."""
    buf = bytearray(1 + len(decoded))
    buf[1:] = decoded
    cobs.encode(buf, 0, 1, len(decoded))
    return bytes(buf)


def _decode(frame: bytes) -> bytes:
    """Decode a wire frame (with leading overhead, no NUL terminator)."""
    buf = bytearray(frame)
    cobs.decode(buf, 0, len(buf))
    assert buf[0] == 0  # leading overhead becomes the first 'zero'
    return bytes(buf[1:])


def test_classic_paper_vector_1():
    # decoded:  0x00
    # encoded:  0x01 0x01
    assert _round_trip(b"\x00") == b"\x01\x01"


def test_classic_paper_vector_2():
    # decoded:  0x00 0x00
    # encoded:  0x01 0x01 0x01
    assert _round_trip(b"\x00\x00") == b"\x01\x01\x01"


def test_classic_paper_vector_3():
    # decoded:  0x11 0x22 0x00 0x33
    # encoded:  0x03 0x11 0x22 0x02 0x33
    assert _round_trip(b"\x11\x22\x00\x33") == b"\x03\x11\x22\x02\x33"


def test_classic_paper_vector_4():
    # decoded:  0x11 0x22 0x33 0x44
    # encoded:  0x05 0x11 0x22 0x33 0x44
    assert _round_trip(b"\x11\x22\x33\x44") == b"\x05\x11\x22\x33\x44"


def test_round_trip_random_bytes():
    payload = bytes(range(50))
    frame = _round_trip(payload)
    assert _decode(frame) == payload


def test_round_trip_with_internal_zeros():
    payload = b"\x00\x10\x20\x00\x30\x00\x00\x40"
    frame = _round_trip(payload)
    assert _decode(frame) == payload


def test_decode_rejects_internal_nul():
    bad = bytearray(b"\x03\x11\x22\x00\x33")
    try:
        cobs.decode(bad, 0, len(bad))
    except ValueError:
        return
    raise AssertionError("expected ValueError on internal NUL")
