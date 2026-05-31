"""Pin the compat shim's (x,y)→index and (x,y,z)→index mappings to the
upstream foundry-daemon implementation.

The reference is
``ref_data/lumicube-daemon/.../python/foundry_api/standard_library.py``,
methods ``_Display.set_leds`` and ``_Display.set_3d`` (also duplicated as
``display_set_leds`` / ``display_set_3d`` in the daemon's server-side
service). If pylumicube's shim disagrees with those formulae, community
scripts will light the wrong LEDs — that's the whole point of this file.
"""

from __future__ import annotations

import pytest

from pylumicube.compat.runtime import (
    _coerce_index,
    _xy_to_index,
    _xyz_to_index,
)


# --- 2D (x, y) → LED index ---------------------------------------------


def _expected_xy(x: int, y: int) -> int | None:
    """The upstream formula, copied verbatim from standard_library.py."""
    if x < 8 and y < 8:
        return 63 - x - 8 * y
    elif x < 16 and y < 8:
        return 7 - y + 8 * x
    elif x < 8 and y < 16:
        return 176 + y - 8 * x
    return None


@pytest.mark.parametrize("x", range(16))
@pytest.mark.parametrize("y", range(16))
def test_xy_to_index_matches_upstream(x: int, y: int) -> None:
    assert _xy_to_index(x, y) == _expected_xy(x, y)


def test_xy_corner_invalid_returns_none() -> None:
    # The (x>=8, y>=8) quadrant is unmapped in upstream — silently dropped.
    for x in range(8, 16):
        for y in range(8, 16):
            assert _xy_to_index(x, y) is None


def test_xy_left_panel_anchor() -> None:
    # Upstream: (0,0) → 63, (7,0) → 56, (0,7) → 7, (7,7) → 0
    assert _xy_to_index(0, 0) == 63
    assert _xy_to_index(7, 0) == 56
    assert _xy_to_index(0, 7) == 7
    assert _xy_to_index(7, 7) == 0


# --- 3D (x, y, z) → LED index ------------------------------------------


def _expected_xyz(x: int, y: int, z: int) -> int | None:
    if x < 8 and y < 8 and z == 8:
        return 63 - x - 8 * y
    elif y < 8 and z < 8 and x == 8:
        return 127 - y - 8 * z
    elif z < 8 and x < 8 and y == 8:
        return 191 - z - 8 * x
    return None


def test_xyz_left_face() -> None:
    for x in range(8):
        for y in range(8):
            assert _xyz_to_index(x, y, 8) == _expected_xyz(x, y, 8)


def test_xyz_right_face() -> None:
    for y in range(8):
        for z in range(8):
            assert _xyz_to_index(8, y, z) == _expected_xyz(8, y, z)


def test_xyz_top_face() -> None:
    for x in range(8):
        for z in range(8):
            assert _xyz_to_index(x, 8, z) == _expected_xyz(x, 8, z)


def test_xyz_off_face_returns_none() -> None:
    # Interior coords are not on any face.
    assert _xyz_to_index(0, 0, 0) is None
    assert _xyz_to_index(3, 3, 3) is None
    # Out-of-range coords.
    assert _xyz_to_index(9, 0, 8) is None
    assert _xyz_to_index(0, 9, 8) is None


# --- _coerce_index dispatch --------------------------------------------


def test_coerce_int_in_range() -> None:
    assert _coerce_index(0) == 0
    assert _coerce_index(191) == 191


def test_coerce_int_out_of_range() -> None:
    assert _coerce_index(-1) is None
    assert _coerce_index(192) is None


def test_coerce_2tuple() -> None:
    assert _coerce_index((0, 0)) == 63
    assert _coerce_index((7, 7)) == 0


def test_coerce_3tuple() -> None:
    assert _coerce_index((0, 0, 8)) == 63
    assert _coerce_index((8, 0, 0)) == 127


def test_coerce_garbage() -> None:
    assert _coerce_index("nope") is None
    assert _coerce_index((1,)) is None
    assert _coerce_index((1, 2, 3, 4)) is None
