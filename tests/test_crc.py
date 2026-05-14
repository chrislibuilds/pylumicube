"""CRC-16/CCITT-FALSE — known-good vectors from the Java CRC16 lookup table."""

from pylumicube.crc import calculate


def test_empty():
    assert calculate(b"") == 0xFFFF


def test_single_zero():
    assert calculate(b"\x00") == 0xE1F0


def test_single_one():
    assert calculate(b"\x01") == 0xF1D1


def test_one_two():
    assert calculate(b"\x01\x02") == 0x0E7C


def test_zero_to_three():
    assert calculate(b"\x00\x01\x02\x03") == 0xE5F1


def test_offset_length():
    data = b"\xff\x01\x02\x03\xff"
    assert calculate(data, offset=1, length=2) == 0x0E7C
