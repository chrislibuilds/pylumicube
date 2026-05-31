"""Compat shim end-to-end checks against a fake Display.

We don't open a serial port — the focus is the namespace shape, stub
behaviour, and that DisplayShim/set_panel actually forwards the right
LED indices to the wrapped Display.
"""

from __future__ import annotations

import warnings
from unittest.mock import MagicMock

import pytest

from pylumicube.compat import build_globals, get_hosted_cube, open_or_use_hosted
from pylumicube.compat.runtime import (
    DisplayShim,
    LumiCubeCompat,
    StubModule,
    _set_hosted_cube,
    hsv_colour,
    random_colour,
)


# ---- DisplayShim wiring ------------------------------------------------


def _make_shim() -> tuple[DisplayShim, MagicMock]:
    fake_display = MagicMock()
    return DisplayShim(fake_display), fake_display


def test_set_led_calls_set_leds_with_correct_index() -> None:
    shim, fake = _make_shim()
    shim.set_led(0, 0, 0xFF0000)
    # (0,0) maps to LED index 63 per upstream.
    fake.set_leds.assert_called_once_with({63: 0xFF0000}, show=True)


def test_set_leds_dict_int_keys_pass_through() -> None:
    shim, fake = _make_shim()
    shim.set_leds({0: 0x123456, 191: 0x654321})
    fake.set_leds.assert_called_once_with({0: 0x123456, 191: 0x654321}, show=True)


def test_set_leds_dict_2tuple_keys() -> None:
    shim, fake = _make_shim()
    shim.set_leds({(0, 0): 0xFF0000, (7, 7): 0x00FF00})
    fake.set_leds.assert_called_once_with({63: 0xFF0000, 0: 0x00FF00}, show=True)


def test_set_leds_dict_3tuple_keys() -> None:
    shim, fake = _make_shim()
    shim.set_leds({(0, 0, 8): 0xAABBCC, (8, 0, 0): 0xDDEEFF})
    fake.set_leds.assert_called_once_with({63: 0xAABBCC, 127: 0xDDEEFF}, show=True)


def test_set_leds_drops_invalid_coords() -> None:
    shim, fake = _make_shim()
    # (8, 8) is the unmapped quadrant — should be silently dropped.
    shim.set_leds({(0, 0): 0xFF0000, (8, 8): 0x00FF00})
    fake.set_leds.assert_called_once_with({63: 0xFF0000}, show=True)


def test_set_all_uses_fill() -> None:
    shim, fake = _make_shim()
    shim.set_all(0x00FF00)
    fake.fill.assert_called_once_with(0x00FF00, show=True)


def test_set_panel_left_anchor() -> None:
    """Upstream: ``set_panel('left', rows)`` starts at (0, 7) and y descends.
    First row goes to y=7 (LEDs 56..63 in (x,y) space)."""
    shim, fake = _make_shim()
    # 8 rows of 8 colours, all = 0xAAA so we can pick the indices off.
    rows = [[0xAAAAAA] * 8 for _ in range(8)]
    shim.set_panel("left", rows)
    sent = fake.set_leds.call_args[0][0]
    # All 64 LEDs on the left panel (indices 0..63) should be lit.
    assert set(sent.keys()) == set(range(64))


def test_set_panel_invalid_name() -> None:
    shim, _ = _make_shim()
    with pytest.raises(ValueError):
        shim.set_panel("nope", [[0]])


def test_brightness_round_trip_via_property() -> None:
    shim, fake = _make_shim()
    shim.brightness = 50
    # 50/100 → 128/255 (rounded)
    fake.set_brightness.assert_called_once_with(128)
    assert shim.brightness == 50


def test_brightness_clamped() -> None:
    shim, _ = _make_shim()
    shim.brightness = 250
    assert shim.brightness == 100
    shim.brightness = -10
    assert shim.brightness == 0


# ---- helpers -----------------------------------------------------------


def test_hsv_colour_pure_red() -> None:
    assert hsv_colour(0.0, 1.0, 1.0) == 0xFF0000


def test_hsv_colour_pure_green() -> None:
    assert hsv_colour(1 / 3, 1.0, 1.0) == 0x00FF00


def test_hsv_colour_pure_blue() -> None:
    assert hsv_colour(2 / 3, 1.0, 1.0) == 0x0000FF


def test_hsv_colour_clamps() -> None:
    # Negative saturation/value should clamp to 0, producing black.
    assert hsv_colour(0.5, -1, -1) == 0x000000


def test_random_colour_is_24bit() -> None:
    for _ in range(50):
        c = random_colour()
        assert 0 <= c <= 0xFFFFFF


# ---- StubModule --------------------------------------------------------


def test_stub_method_returns_none() -> None:
    stub = StubModule("speaker")
    # First call triggers a warning, but the call itself returns None.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = stub.say("hi")
    assert result is None
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_stub_field_read_returns_zero_for_known_fields() -> None:
    stub = StubModule("buttons", available_fields=("top_pressed", "top_pressed_count"))
    # `if buttons.top_pressed:` must be falsey.
    assert not stub.top_pressed
    # `buttons.top_pressed_count % 2` must not blow up.
    assert stub.top_pressed_count % 2 == 0


def test_stub_method_attr_is_callable() -> None:
    stub = StubModule("buttons", available_fields=("top_pressed",))
    # `get_next_action` is not a declared field → must be a callable that returns None.
    handle = stub.get_next_action
    assert callable(handle)
    assert handle(timeout=1) is None


def test_stub_field_write_is_accepted() -> None:
    stub = StubModule("speaker", available_fields=("volume",))
    # `speaker.volume = 10` must not raise.
    stub.volume = 10


def test_stub_warns_only_once_per_attr() -> None:
    StubModule._warned.clear()
    stub = StubModule("env_sensor", available_fields=("humidity",))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _ = stub.humidity
        _ = stub.humidity
        _ = stub.humidity
    runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    assert len(runtime_warnings) == 1


# ---- Namespace shape ---------------------------------------------------


def _make_cube_compat() -> LumiCubeCompat:
    """Build a LumiCubeCompat without opening a real serial port."""
    fake_cube = MagicMock()
    fake_cube.display = MagicMock()
    return LumiCubeCompat(fake_cube)


def test_build_globals_has_expected_names() -> None:
    ns = build_globals(_make_cube_compat())
    # Modules
    for name in ("time", "math", "random"):
        assert name in ns
    # Facade + per-module aliases
    for name in ("cube", "display", "buttons", "light_sensor", "env_sensor",
                 "imu", "screen", "speaker", "microphone", "pi"):
        assert name in ns
    # Colours
    for name in ("black", "white", "red", "orange", "yellow", "green",
                 "cyan", "blue", "magenta", "pink", "purple", "grey"):
        assert isinstance(ns[name], int)
    # Helpers
    for name in ("hsv_colour", "random_colour", "noise_2d", "noise_3d",
                 "noise_4d", "run_async"):
        assert callable(ns[name])
    # Speaker waveform sentinels
    assert ns["sine_wave"] == "sine_wave"
    assert ns["white_noise"] == "white_noise"
    # Class
    assert ns["LumiCube"] is LumiCubeCompat
    assert ns["Cube"] is LumiCubeCompat


def test_build_globals_display_alias_matches_cube_display() -> None:
    compat = _make_cube_compat()
    ns = build_globals(compat)
    assert ns["display"] is compat.display
    assert ns["cube"] is compat


# ---- scroll_text smoke test --------------------------------------------


def test_scroll_text_emits_frames_and_clears() -> None:
    """scroll_text should call set_leds at least once per shifted column.

    We don't validate the exact pixel layout (font choice is internal),
    just that the loop executes and the wrapped Display sees writes.
    """
    shim, fake = _make_shim()
    shim.scroll_text("A", speed=1000)  # speed=1000 → near-zero sleep
    assert fake.set_leds.call_count > 0


# ---- Hosted-cube registry ----------------------------------------------


def test_get_hosted_cube_is_none_by_default() -> None:
    _set_hosted_cube(None)
    assert get_hosted_cube() is None


def test_set_hosted_cube_round_trip() -> None:
    fake_cube = MagicMock()
    _set_hosted_cube(fake_cube)
    try:
        assert get_hosted_cube() is fake_cube
    finally:
        _set_hosted_cube(None)
    assert get_hosted_cube() is None


def test_open_or_use_hosted_yields_hosted_cube_without_opening_serial() -> None:
    """When a hosted cube is registered, the helper must yield it and
    NOT instantiate `LumiCube(...)` — that would try to open a serial port."""
    fake_cube = MagicMock()
    _set_hosted_cube(fake_cube)
    try:
        with open_or_use_hosted("/dev/does-not-exist") as cube:
            assert cube is fake_cube
    finally:
        _set_hosted_cube(None)
    # The fake cube must not have been touched as a context manager —
    # ownership stays with lumicube-run.
    fake_cube.__enter__.assert_not_called()
    fake_cube.__exit__.assert_not_called()


def test_open_or_use_hosted_opens_fresh_lumicube_when_unhosted(monkeypatch) -> None:
    """Without a hosted cube the helper must defer to `LumiCube(port)`."""
    _set_hosted_cube(None)

    opened: list[str] = []

    class FakeCube:
        def __init__(self, port: str, **_) -> None:
            opened.append(port)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("pylumicube.compat.runtime.LumiCube", FakeCube)
    with open_or_use_hosted("/dev/test") as cube:
        assert isinstance(cube, FakeCube)
    assert opened == ["/dev/test"]
