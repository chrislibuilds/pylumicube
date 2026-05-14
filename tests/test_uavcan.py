"""UAVCAN messageId encode/decode + the captured strace example."""

from pylumicube import uavcan as u
from pylumicube.constants import DEFAULT_PRIORITY, TYPE_SET_FIELDS


def test_request_encoding_matches_capture():
    """Captured frame: src=1 dst=124 type=216 prio=20 yields 0x14d8fc81."""
    mid = u.encode_request(source_id=1, dest_id=124, type_id=TYPE_SET_FIELDS, priority=DEFAULT_PRIORITY)
    le_bytes = mid.to_bytes(4, "little")
    assert le_bytes == bytes.fromhex("81fcd814")


def test_response_decoding():
    mid = u.encode_response(source_id=124, dest_id=1, type_id=216, priority=20)
    le_bytes = mid.to_bytes(4, "little")
    # response: source=124(0x7c), dest=1, type=216(0xd8), prio=20
    # byte0 = src|0x80 = 0xfc; byte1 = dest|0x00 = 0x01; byte2 = 0xd8; byte3 = 0x14
    assert le_bytes == bytes([0xFC, 0x01, 0xD8, 0x14])


def test_anonymous_broadcast():
    mid = u.encode_anonymous_broadcast(type_id=1, priority=20)
    # source=0, type fits in 2 bits, prio=20
    le_bytes = mid.to_bytes(4, "little")
    assert le_bytes == bytes([0x00, 0x01, 0x00, 0x14])


def test_pack_message_layout():
    body = u.pack_message(transfer_id=2, message_id=0x14d8fc81, payload=b"\xaa\xbb")
    assert body == bytes.fromhex("0281fcd814aabb")


def test_parse_request_round_trip():
    mid = u.encode_request(source_id=1, dest_id=124, type_id=216, priority=20)
    body = u.pack_message(transfer_id=5, message_id=mid, payload=b"\xde\xad")
    parsed = u.parse_message(body)
    assert isinstance(parsed, u.ServiceRequest)
    assert parsed.source_id == 1
    assert parsed.dest_id == 124
    assert parsed.type_id == 216
    assert parsed.priority == 20
    assert parsed.transfer_id == 5
    assert parsed.payload == b"\xde\xad"


def test_parse_broadcast_round_trip():
    mid = u.encode_broadcast(source_id=124, type_id=20_000, priority=20)
    body = u.pack_message(transfer_id=3, message_id=mid, payload=b"\x01\x02\x03")
    parsed = u.parse_message(body)
    assert isinstance(parsed, u.Broadcast)
    assert parsed.source_id == 124
    assert parsed.type_id == 20_000
    assert parsed.transfer_id == 3
    assert parsed.payload == b"\x01\x02\x03"


def test_parse_anonymous_broadcast():
    mid = u.encode_anonymous_broadcast(type_id=1, priority=20)
    body = u.pack_message(transfer_id=0, message_id=mid, payload=b"\x01" + b"\xaa" * 6)
    parsed = u.parse_message(body)
    assert isinstance(parsed, u.Broadcast)
    assert parsed.source_id == 0
    assert parsed.type_id == 1
