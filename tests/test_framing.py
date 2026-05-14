"""Frame builders + parser round-trips."""

from pylumicube import framing
from pylumicube.constants import (
    CMD_ACKNOWLEDGE,
    CMD_INITIALISE,
    CMD_INITIALISED,
    CMD_MESSAGE,
    CMD_PING,
    CMD_PONG,
    PROTOCOL_VERSION,
)


def _decode_one(frame_bytes: bytes) -> framing.DecodedFrame:
    stream = framing.FrameStream()
    stream.feed(frame_bytes)
    decoded = stream.pop()
    assert decoded is not None, "stream produced no frame"
    return decoded


def test_ping_round_trip():
    decoded = _decode_one(framing.build_ping())
    assert decoded.command == CMD_PING
    assert decoded.body == b""


def test_pong_round_trip():
    decoded = _decode_one(framing.build_pong())
    assert decoded.command == CMD_PONG
    assert decoded.body == bytes([PROTOCOL_VERSION])


def test_initialise_round_trip():
    decoded = _decode_one(framing.build_initialise(sequence=0x42))
    assert decoded.command == CMD_INITIALISE
    assert decoded.body == bytes([PROTOCOL_VERSION, 0x42])


def test_initialised_round_trip():
    decoded = _decode_one(framing.build_initialised())
    assert decoded.command == CMD_INITIALISED
    assert decoded.body == b""


def test_acknowledge_round_trip():
    decoded = _decode_one(framing.build_acknowledge(0xAB))
    assert decoded.command == CMD_ACKNOWLEDGE
    assert decoded.body == bytes([0xAB])


def test_message_round_trip():
    payload = b"\x02\x81\xfc\xd8\x14\x11\x80\x01\x70" + b"\x00" * 5
    decoded = _decode_one(framing.build_message(payload, sequence=0x05))
    assert decoded.command == CMD_MESSAGE
    # The body parser leaves the trailing sequence in the body; the link
    # layer is responsible for stripping it.
    assert decoded.body == payload + bytes([0x05])


def test_stream_handles_garbage_then_frame():
    stream = framing.FrameStream()
    stream.feed(b"\x01\x02\x03\x00")  # bogus prefix terminated by delimiter
    assert stream.pop() is None
    stream.feed(framing.build_ping())
    decoded = stream.pop()
    assert decoded is not None
    assert decoded.command == CMD_PING


def test_stream_drops_corrupt_crc():
    frame = bytearray(framing.build_ping())
    # Corrupt one byte in the COBS region (not the leading delimiter)
    frame[2] ^= 0x55
    stream = framing.FrameStream()
    stream.feed(bytes(frame))
    assert stream.pop() is None
