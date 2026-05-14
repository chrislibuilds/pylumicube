"""Field metadata structures.

The wire format encodes values according to per-field metadata: the type
(FieldType enum), the size in bytes, the floor key of a homogeneous block
and the span. Real metadata is discovered at runtime via ENUMERATE_FIELDS
(PROTOCOL §4.5), but for simple SET_FIELDS use cases we hardcode the
display-module schema we already know.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    FIELD_TYPE_BOOLEAN,
    FIELD_TYPE_FLOAT,
    FIELD_TYPE_INT,
    FIELD_TYPE_RAW,
    FIELD_TYPE_UINT,
    FIELD_TYPE_UTF8_CHAR,
)


@dataclass(frozen=True)
class FieldSpec:
    """Describes one homogeneous block of fields keyed [floor .. floor+span)."""
    floor: int
    span: int
    type: int   # FieldType enum
    size: int   # bytes per value (0 = variable)
    name: str = ""

    @property
    def signed(self) -> bool:
        return self.type in (FIELD_TYPE_INT,)


def display_specs() -> dict[int, FieldSpec]:
    """Display module schema for the `cube` base board, verified at runtime
    via ENUMERATE_FIELDS. Floor key -> FieldSpec.

    The cube node has multiple modules (microphone, speaker, screen, etc.)
    so display fields don't start at key 0 — they live at offset 256.

    The firmware allocates `led_colour` with span = MAX_LEDS-1 = 999, even
    though the physical panel is only 192 LEDs (24x8). Writes past LED 191
    are accepted but discarded.

    | Key range  | Field                    | Size | Type    |
    | 256        | brightness               | 1    | UINT    |
    | 257..265   | panel_*, refresh, gamma  | 1-2  | UINT/BOOL |
    | 266..1264  | led_colour (192 LEDs)    | 3    | UINT    |
    | 1265       | spread_spectrum_period   | 2    | UINT    |
    | 1266       | show                     | 1    | BOOLEAN |
    """
    return {
        256:  FieldSpec(256,  1,   FIELD_TYPE_UINT,    1, "brightness"),
        257:  FieldSpec(257,  1,   FIELD_TYPE_UINT,    2, "panel_width"),
        258:  FieldSpec(258,  1,   FIELD_TYPE_UINT,    2, "panel_height"),
        259:  FieldSpec(259,  1,   FIELD_TYPE_UINT,    2, "refresh_period"),
        260:  FieldSpec(260,  1,   FIELD_TYPE_UINT,    2, "estimated_current"),
        261:  FieldSpec(261,  1,   FIELD_TYPE_UINT,    2, "max_current"),
        262:  FieldSpec(262,  1,   FIELD_TYPE_BOOLEAN, 1, "gamma_correction_enabled"),
        263:  FieldSpec(263,  1,   FIELD_TYPE_UINT,    1, "gamma_correction_red"),
        264:  FieldSpec(264,  1,   FIELD_TYPE_UINT,    1, "gamma_correction_green"),
        265:  FieldSpec(265,  1,   FIELD_TYPE_UINT,    1, "gamma_correction_blue"),
        266:  FieldSpec(266,  999, FIELD_TYPE_UINT,    3, "led_colour"),
        1265: FieldSpec(1265, 1,   FIELD_TYPE_UINT,    2, "spread_spectrum_period"),
        1266: FieldSpec(1266, 1,   FIELD_TYPE_BOOLEAN, 1, "show"),
    }
