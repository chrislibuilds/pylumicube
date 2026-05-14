"""FlatDictionary serialiser tests, including the format fix from PROTOCOL §4.8.

The high nibble of a command byte is the discriminator (0=run, 1=skip);
the low nibble is the parameter width in bytes (NOT width-1, which was
the bug in the previous implementation).
"""

from pylumicube import flat_dictionary
from pylumicube.metadata import display_specs, FieldSpec
from pylumicube.constants import (
    FIELD_TYPE_BOOLEAN,
    FIELD_TYPE_RAW,
    FIELD_TYPE_UINT,
)


def test_skip_then_run_command_bytes():
    """Example from PROTOCOL.md §4.8: 'skip 5, run 1, value' -> 0x11 0x05 0x01 0x01 ..."""
    specs = {
        0: FieldSpec(0, 100, FIELD_TYPE_UINT, 1, "test"),
    }
    encoded = flat_dictionary.serialise({5: 0xAA}, specs)
    assert encoded == bytes([0x11, 0x05, 0x01, 0x01, 0xAA])


def test_two_value_run():
    specs = {0: FieldSpec(0, 100, FIELD_TYPE_UINT, 1, "test")}
    encoded = flat_dictionary.serialise({3: 0x10, 4: 0x20}, specs)
    # skip 3 (width 1), run 2 (width 1), 0x10 0x20
    assert encoded == bytes([0x11, 0x03, 0x01, 0x02, 0x10, 0x20])


def test_single_show_at_key_1266():
    """Setting just `show` to true on the cube node's display module."""
    encoded = flat_dictionary.serialise({1266: 1}, display_specs())
    # skip 1266 (width 2: f2 04), run 1 (width 1), value 1
    assert encoded == bytes([0x12, 0xF2, 0x04, 0x01, 0x01, 0x01])


def test_set_one_led_red_and_show():
    """Set LED 0 (key 266) to RGB 0xFF0000 and trigger show (key 1266).
    Note: 0xFF0000 in 3-byte LE is 00 00 ff."""
    encoded = flat_dictionary.serialise({266: 0xFF0000, 1266: 1}, display_specs())
    expected = bytes([
        0x12, 0x0A, 0x01,       # skip 266 (width 2)
        0x01, 0x01,             # run length 1 (width 1)
        0x00, 0x00, 0xFF,       # RGB value
        0x12, 0xE7, 0x03,       # skip 999 (key 267 -> 1266)
        0x01, 0x01,             # run length 1
        0x01,                   # show = true
    ])
    assert encoded == expected


def test_skip_with_two_byte_width():
    specs = {500: FieldSpec(500, 1, FIELD_TYPE_UINT, 1, "x")}
    encoded = flat_dictionary.serialise({500: 0x55}, specs)
    # skip 500 needs width 2 (500 > 255)
    assert encoded == bytes([0x12, 0xF4, 0x01, 0x01, 0x01, 0x55])


def test_round_trip_via_decoder():
    encoded = flat_dictionary.serialise({256: 200, 257: 0x1234}, display_specs())
    decoded = flat_dictionary.deserialise(encoded, display_specs())
    assert decoded[256] == 200
    assert decoded[257] == 0x1234


def test_run_split_across_metadata_blocks():
    """A run that spans two metadata floors must keep its single run header
    but emit values according to each block's spec."""
    specs = {
        0: FieldSpec(0, 1, FIELD_TYPE_UINT, 1, "a"),
        1: FieldSpec(1, 2, FIELD_TYPE_UINT, 2, "b"),
    }
    encoded = flat_dictionary.serialise({0: 0x10, 1: 0x2030, 2: 0x4050}, specs)
    expected = bytes([
        0x01, 0x03,                 # run length 3
        0x10,                       # 1-byte
        0x30, 0x20, 0x50, 0x40,     # 2-byte LE
    ])
    assert encoded == expected
