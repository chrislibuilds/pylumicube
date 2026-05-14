"""UAVCAN-style message ID encoding/decoding.

A `MESSAGE` frame body (after stripping the 0x2D command and trailing
sequence/CRC) is:

    [ transferId (1 B) ][ messageId (4 B little-endian) ][ payload ... ]

The 32-bit messageId discriminates four kinds of transfer. See PROTOCOL.md §3.1.
"""

from __future__ import annotations

from dataclasses import dataclass


# --------------------- encoding ---------------------

def encode_broadcast(source_id: int, type_id: int, priority: int) -> int:
    """sourceId in [1..127], typeId is 16-bit. PROTOCOL §3.1.1."""
    if source_id == 0:
        raise ValueError("use encode_anonymous_broadcast for sourceId == 0")
    return (
        (source_id & 0x7F)
        | ((type_id & 0xFFFF) << 8)
        | ((priority & 0x1F) << 24)
    )


def encode_anonymous_broadcast(type_id: int, priority: int) -> int:
    """typeId is 2 bits when sourceId == 0. PROTOCOL §3.1.2."""
    return ((type_id & 0x03) << 8) | ((priority & 0x1F) << 24)


def encode_request(source_id: int, dest_id: int, type_id: int, priority: int) -> int:
    """PROTOCOL §3.1.3."""
    return (
        0x8080
        | (source_id & 0x7F)
        | ((dest_id & 0x7F) << 8)
        | ((type_id & 0xFF) << 16)
        | ((priority & 0x1F) << 24)
    )


def encode_response(source_id: int, dest_id: int, type_id: int, priority: int) -> int:
    """PROTOCOL §3.1.4."""
    return (
        0x80
        | (source_id & 0x7F)
        | ((dest_id & 0x7F) << 8)
        | ((type_id & 0xFF) << 16)
        | ((priority & 0x1F) << 24)
    )


def pack_message(transfer_id: int, message_id: int, payload: bytes) -> bytes:
    """Build the inner UAVCAN payload that goes inside a link-layer MESSAGE."""
    return bytes(
        [
            transfer_id & 0x1F,
            message_id & 0xFF,
            (message_id >> 8) & 0xFF,
            (message_id >> 16) & 0xFF,
            (message_id >> 24) & 0xFF,
        ]
    ) + payload


# --------------------- decoding ---------------------

@dataclass(frozen=True)
class Broadcast:
    source_id: int      # 0 for anonymous
    type_id: int
    transfer_id: int
    priority: int
    payload: bytes


@dataclass(frozen=True)
class ServiceRequest:
    source_id: int
    dest_id: int
    type_id: int
    transfer_id: int
    priority: int
    payload: bytes


@dataclass(frozen=True)
class ServiceResponse:
    source_id: int
    dest_id: int
    type_id: int
    transfer_id: int
    priority: int
    payload: bytes


Transfer = Broadcast | ServiceRequest | ServiceResponse


def parse_message(message_body: bytes) -> Transfer | None:
    """Decode a MESSAGE frame body (the bytes between 0x2D and the trailing
    sequence byte; see SerialConnector.receiveFrame in the Java source)."""
    if len(message_body) < 5:
        return None
    transfer_id = message_body[0] & 0x1F
    b0, b1, b2, b3 = message_body[1], message_body[2], message_body[3], message_body[4]
    payload = message_body[5:]
    priority = b3 & 0x1F
    if (b0 & 0x80) == 0:
        # broadcast (named or anonymous)
        source_id = b0 & 0x7F
        if source_id == 0:
            type_id = b1 & 0x03
        else:
            type_id = b1 | (b2 << 8)
        return Broadcast(source_id=source_id, type_id=type_id, transfer_id=transfer_id,
                         priority=priority, payload=payload)
    source_id = b0 & 0x7F
    dest_id = b1 & 0x7F
    type_id = b2
    if (b1 & 0x80) == 0:
        return ServiceResponse(source_id=source_id, dest_id=dest_id, type_id=type_id,
                               transfer_id=transfer_id, priority=priority, payload=payload)
    return ServiceRequest(source_id=source_id, dest_id=dest_id, type_id=type_id,
                          transfer_id=transfer_id, priority=priority, payload=payload)
