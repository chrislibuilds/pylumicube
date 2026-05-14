"""Frame builders and decoder for the LumiCube link layer.

Frame layout on the wire (between two 0x00 delimiters):

    [ COBS overhead ][ command ][ ... body ... ][ CRC16 hi ][ CRC16 lo ]

CRC covers `command + body + crc` such that the recomputed CRC over those
bytes is 0 on a valid frame. See PROTOCOL.md §2.1.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import cobs, crc as crc16
from .constants import (
    CMD_ACKNOWLEDGE,
    CMD_INITIALISE,
    CMD_INITIALISED,
    CMD_MESSAGE,
    CMD_PING,
    CMD_PONG,
    CMD_UNINITIALISED,
    PROTOCOL_VERSION,
)


@dataclass(frozen=True)
class DecodedFrame:
    command: int
    body: bytes        # everything between command and CRC (no sequence stripping)


def _seal(body_with_command: bytes) -> bytes:
    """Wrap a [command + body] payload into a delimited COBS frame.

    Layout pre-COBS: [overhead placeholder][cmd][body][crc-hi][crc-lo]
    """
    if len(body_with_command) + 3 > 255:
        # +3 accounts for the COBS overhead byte and 2-byte CRC; +1 more delimiter
        raise ValueError("frame body too large")
    inner_len = len(body_with_command) + 2  # cmd+body+crc
    buf = bytearray(1 + inner_len + 1)       # +1 leading overhead, +1 trailing delimiter
    buf[1 : 1 + len(body_with_command)] = body_with_command
    checksum = crc16.calculate(body_with_command)
    buf[1 + len(body_with_command)] = (checksum >> 8) & 0xFF
    buf[1 + len(body_with_command) + 1] = checksum & 0xFF
    cobs.encode(buf, 0, 1, inner_len)
    buf[1 + inner_len] = 0x00
    return bytes(buf)


def build_ping() -> bytes:
    return _seal(bytes([CMD_PING]))


def build_pong(version: int = PROTOCOL_VERSION) -> bytes:
    return _seal(bytes([CMD_PONG, version & 0xFF]))


def build_initialise(sequence: int, version: int = PROTOCOL_VERSION) -> bytes:
    return _seal(bytes([CMD_INITIALISE, version & 0xFF, sequence & 0xFF]))


def build_initialised() -> bytes:
    return _seal(bytes([CMD_INITIALISED]))


def build_uninitialised() -> bytes:
    return _seal(bytes([CMD_UNINITIALISED]))


def build_acknowledge(sequence: int) -> bytes:
    return _seal(bytes([CMD_ACKNOWLEDGE, sequence & 0xFF]))


def build_message(uavcan_payload: bytes, sequence: int) -> bytes:
    """A MESSAGE frame is [0x2D][uavcan][sequence][CRC]."""
    body = bytes([CMD_MESSAGE]) + uavcan_payload + bytes([sequence & 0xFF])
    return _seal(body)


class FrameStream:
    """Stateful 0x00-delimited frame parser.

    Feed raw bytes via `feed()`, then drain `pop()` to get decoded frames.
    The stream is lenient: corrupted frames are dropped silently, and an
    occasional spurious leading byte (residual from before our handshake)
    is recoverable on the next delimiter.
    """

    def __init__(self) -> None:
        self._scratch = bytearray()
        self._pending: list[DecodedFrame] = []

    def feed(self, data: bytes | bytearray) -> None:
        for byte in data:
            if byte == 0x00:
                if self._scratch:
                    decoded = self._try_decode(bytes(self._scratch))
                    if decoded is not None:
                        self._pending.append(decoded)
                    self._scratch.clear()
            else:
                self._scratch.append(byte)
                if len(self._scratch) > 255:
                    # Oversized frame; drop and recover on next delimiter.
                    self._scratch.clear()

    def pop(self) -> DecodedFrame | None:
        if not self._pending:
            return None
        return self._pending.pop(0)

    def has_frame(self) -> bool:
        return bool(self._pending)

    @staticmethod
    def _try_decode(raw: bytes) -> DecodedFrame | None:
        if len(raw) < 4:
            return None  # min: COBS byte + cmd + 2 CRC
        try:
            buffer = bytearray(raw)
            cobs.decode(buffer, 0, len(buffer))
        except ValueError:
            return None
        # buffer is now [0x00, cmd, body..., crc_hi, crc_lo]; CRC over [cmd..crc_lo] must be 0
        crc_check = crc16.calculate(bytes(buffer[1:]))
        if crc_check != 0:
            return None
        command = buffer[1]
        body = bytes(buffer[2:-2])
        return DecodedFrame(command=command, body=body)
