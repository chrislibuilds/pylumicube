"""Compat runtime — recreates the upstream foundry-daemon globals so
community scripts run unchanged.

The coordinate mappings and method signatures below are pinned to
``ref_data/lumicube-daemon/.../python/foundry_api/standard_library.py``
(the Python module the daemon injects into a user script's namespace).
"""

from __future__ import annotations

import concurrent.futures as _cf
import logging
import math
import random
import sys
import threading
import time
import warnings
from colorsys import hsv_to_rgb
from typing import Any, Callable, Mapping

from ..constants import SERIAL_DEVICE
from ..display import NUM_LEDS, Display
from ..node import LumiCube
from . import font as _font

log = logging.getLogger(__name__)


# ----- colour constants (verbatim from upstream standard_library.py) -----

BLACK = 0x000000
GREY = 0x808080
WHITE = 0xFFFFFF
RED = 0xFF0000
ORANGE = 0xFF8C00
YELLOW = 0xFFFF00
GREEN = 0x00FF00
CYAN = 0x00FFFF
BLUE = 0x0000FF
MAGENTA = 0xFF00FF
PINK = 0xFF007F
PURPLE = 0x800080


# ----- helpers (verbatim semantics from upstream standard_library.py) -----


def hsv_colour(hue: float, sat: float, val: float) -> int:
    hue = hue % 1
    sat = max(0.0, min(float(sat), 1.0))
    val = max(0.0, min(float(val), 1.0))
    r, g, b = hsv_to_rgb(hue, sat, val)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def random_colour() -> int:
    return hsv_colour(random.random(), 1, 1)


# ----- async helper -----

_pool_lock = threading.Lock()
_pool: _cf.ThreadPoolExecutor | None = None


def run_async(task: Callable[..., Any], *args: Any, **kwargs: Any) -> _cf.Future:
    if not callable(task):
        raise ValueError("Expected function, method, or other callable")
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _cf.ThreadPoolExecutor()
        return _pool.submit(task, *args, **kwargs)


# ----- OpenSimplex noise helpers -----

_noise_lock = threading.Lock()
_noise_gen: Any = None


def _ensure_noise() -> Any:
    global _noise_gen
    with _noise_lock:
        if _noise_gen is None:
            try:
                import opensimplex  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "opensimplex is required for noise_2d/3d/4d; "
                    "install with `pip install opensimplex`"
                ) from exc
            # Match upstream seed so noise textures look the same.
            _noise_gen = opensimplex.OpenSimplex(seed=123456)
        return _noise_gen


def noise_2d(x: float, y: float) -> float:
    return _ensure_noise().noise2(x, y)


def noise_3d(x: float, y: float, z: float) -> float:
    return _ensure_noise().noise3(x, y, z)


def noise_4d(x: float, y: float, z: float, w: float) -> float:
    return _ensure_noise().noise4(x, y, z, w)


# ----- waveform sentinels (only sine_wave / white_noise / square_wave) -----

sine_wave = "sine_wave"
square_wave = "square_wave"
white_noise = "white_noise"


# ----- Display shim --------------------------------------------------------


def _xy_to_index(x: int, y: int) -> int | None:
    """Map a 16x16 logical (x, y) coordinate to a 0..191 LED index.

    Pinned to upstream `_Display.set_leds`:
        if x < 8 and y < 8:    63 - x - 8*y
        elif x < 16 and y < 8: 7 - y + 8*x
        elif x < 8 and y < 16: 176 + y - 8*x
        else:                  (silently ignored)
    """
    if x < 0 or y < 0:
        return None
    if x < 8 and y < 8:
        return 63 - x - 8 * y
    if x < 16 and y < 8:
        return 7 - y + 8 * x
    if x < 8 and y < 16:
        return 176 + y - 8 * x
    return None


def _xyz_to_index(x: int, y: int, z: int) -> int | None:
    """Map a 3D (x, y, z) coordinate on a face of the cube to a 0..191 index.

    Pinned to upstream `_Display.set_3d`:
        if x<8 and y<8 and z==8:   63  - x - 8*y
        elif y<8 and z<8 and x==8: 127 - y - 8*z
        elif z<8 and x<8 and y==8: 191 - z - 8*x
    """
    if x < 0 or y < 0 or z < 0:
        return None
    if x < 8 and y < 8 and z == 8:
        return 63 - x - 8 * y
    if y < 8 and z < 8 and x == 8:
        return 127 - y - 8 * z
    if z < 8 and x < 8 and y == 8:
        return 191 - z - 8 * x
    return None


class DisplayShim:
    """Wraps a `pylumicube.Display` with the upstream community-script API.

    Coordinate semantics are pinned to the daemon's
    ``foundry_api/standard_library.py``. Where upstream uses ``run_async``
    to send updates from a background thread, we do the SET_FIELDS write
    inline — pylumicube's transport layer already serialises one frame at
    a time, and inline calls give scripts predictable timing.
    """

    def __init__(self, display: Display) -> None:
        self._display = display
        # Cached so reads of `display.brightness` don't need a wire round-trip;
        # 100 matches the upstream default exposed in api.txt.
        self._brightness = 100

    # ----- field-like properties -----

    @property
    def brightness(self) -> int:
        return self._brightness

    @brightness.setter
    def brightness(self, value: int) -> None:
        value = int(value)
        # Upstream API documents brightness as 0..100; pylumicube's wire
        # field is 0..255. Translate so old scripts behave the same.
        wire = max(0, min(255, round(value * 255 / 100)))
        self._display.set_brightness(wire)
        self._brightness = max(0, min(100, value))

    # ----- LED writers -----

    def set_led(self, x: int, y: int, colour: int, show: bool = True) -> None:
        self.set_leds({(x, y): colour}, show=show)

    def set_leds(self, xy_to_colour: Mapping[Any, int], show: bool = True) -> None:
        leds: dict[int, int] = {}
        for key, colour in xy_to_colour.items():
            idx = _coerce_index(key)
            if idx is None:
                continue
            leds[idx] = colour & 0xFFFFFF
        if not leds:
            return
        self._display.set_leds(leds, show=show)

    def set_all(self, colour: int, show: bool = True) -> None:
        self._display.fill(colour, show=show)

    def set_panel(self, panel: str, colour_array: list[list[int]], show: bool = True) -> None:
        # Verbatim layout from upstream:
        #   left  → start_x = 0, y starts at 7 (descending)
        #   right → start_x = 8, y starts at 7
        #   top   → start_x = 0, y starts at 15
        start_x = 0
        y = 7
        if panel == "top":
            y += 8
        elif panel == "right":
            start_x += 8
        elif panel != "left":
            raise ValueError("The panel must be 'left', 'right', or 'top'")
        leds: dict[tuple[int, int], int] = {}
        for row in colour_array:
            x = start_x
            for colour in row:
                leds[(x, y)] = colour
                x += 1
            y -= 1
        self.set_leds(leds, show=show)

    def set_3d(self, xyz_to_colour: Mapping[tuple[int, int, int], int],
               show: bool = True) -> None:
        leds: dict[int, int] = {}
        for key, colour in xyz_to_colour.items():
            if not isinstance(key, tuple) or len(key) != 3:
                continue
            idx = _xyz_to_index(*key)
            if idx is None:
                continue
            leds[idx] = colour & 0xFFFFFF
        if not leds:
            return
        self._display.set_leds(leds, show=show)

    # ----- text scroller -----

    def scroll_text(self, text: str, colour: int = WHITE,
                    background_colour: int = BLACK, speed: float = 1.0,
                    with_gap: bool = True) -> None:
        """Scroll ``text`` once across the 16×8 LED area.

        Lays the rendered glyphs into a row buffer 16 columns wider than
        the visible area (so the text enters from the right and exits to
        the left), then walks an 8-row window across it at
        ``0.05 / speed`` seconds per column. Matches the upstream
        ``display_scroll_text`` (PIL-based) behaviour closely enough for
        community-script use.
        """
        # Top padding so the 7-row glyph sits on rows 1..7 (row 0 stays blank);
        # upstream draws into an 8-row image with text at y = 7-height.
        glyph_rows = _font.render_text(text)
        text_width = len(glyph_rows[0])
        # Empty image is wider than visible area on both sides so text
        # enters/exits cleanly.
        pad = 16
        width = text_width + pad * 2
        rows = [[0] * width for _ in range(8)]
        for r, glyph_row in enumerate(glyph_rows):
            target_row = r + 1  # one-pixel top margin → fits in 8-tall area
            if target_row >= 8:
                continue
            for x, pixel in enumerate(glyph_row):
                rows[target_row][pad + x] = pixel

        period = 0.05 / max(speed, 0.0001)
        for shift in range(width - 16):
            frame_start = time.time()
            window = [row[shift : shift + 17] for row in rows]
            leds: dict[tuple[int, int], int] = {}
            for r, row in enumerate(window):
                for x, pixel in enumerate(row):
                    if with_gap and x > 8:
                        plotted_x = x - 1
                    else:
                        plotted_x = x
                    if plotted_x >= 16:
                        continue
                    leds[(plotted_x, 7 - r)] = colour if pixel else background_colour
            self.set_leds(leds)
            dt = time.time() - frame_start
            if dt < period:
                time.sleep(period - dt)


def _coerce_index(key: Any) -> int | None:
    """Normalise a set_leds key into a 0..191 LED index.

    Accepts:
      * int                — used directly (range-checked).
      * (x, y) 2-tuple     — upstream 16×16 mapping.
      * (x, y, z) 3-tuple  — falls through to the 3D mapping.
    Anything else → None (silently dropped, mirroring upstream).
    """
    if isinstance(key, int):
        if 0 <= key < NUM_LEDS:
            return key
        return None
    if isinstance(key, tuple):
        if len(key) == 2:
            return _xy_to_index(int(key[0]), int(key[1]))
        if len(key) == 3:
            return _xyz_to_index(int(key[0]), int(key[1]), int(key[2]))
    return None


# ----- Stubs for not-yet-implemented modules ------------------------------


class StubModule:
    """Placeholder for a hardware module we haven't wired up yet.

    Attribute lookups split on the ``available_fields`` allowlist:
      * Names *in* the allowlist are treated as data fields and return
        plain ``0`` (numeric, falsey, supports arithmetic — matches what
        a freshly-booted sensor reports before its first reading).
      * Names *not* in the allowlist are treated as method handles and
        return a callable that accepts any args and returns ``None``.

    Either path emits a one-time RuntimeWarning per attribute on first
    access. Scripts that only poke unimplemented modules degrade
    silently after the warning instead of crashing.
    """

    _warned: set[str] = set()

    def __init__(self, module_name: str, *, available_fields: tuple[str, ...] = ()) -> None:
        self._module_name = module_name
        self._available_fields = set(available_fields)

    def _warn(self, what: str) -> None:
        key = f"{self._module_name}.{what}"
        if key in StubModule._warned:
            return
        StubModule._warned.add(key)
        msg = (
            f"pylumicube compat: '{self._module_name}.{what}' is not yet "
            f"implemented; returning a no-op value. Script may not behave correctly."
        )
        warnings.warn(msg, RuntimeWarning, stacklevel=3)
        log.warning(msg)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for missing attrs, so this is safe.
        self._warn(name)
        if name in self._available_fields:
            # Numeric zero behaves correctly under arithmetic (`% 2`,
            # `+= 1`, `> threshold`, etc.) and is falsey for `if`-checks.
            return 0
        return _StubCallable(self._module_name, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._warn(name)
        # Silently accept the write so e.g. `speaker.volume = 10` doesn't blow up.


class _StubCallable:
    """Method-handle stub: callable, returns None, falsey for bool checks."""

    def __init__(self, module: str, attr: str) -> None:
        self._module = module
        self._attr = attr

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"<stub {self._module}.{self._attr}>"

    def __eq__(self, other: Any) -> bool:
        return other is None or isinstance(other, _StubCallable)

    def __hash__(self) -> int:
        return hash((self._module, self._attr))


# ----- Top-level facade ----------------------------------------------------


class LumiCubeCompat:
    """The ``cube`` object exposed to community scripts.

    Attributes match upstream's ``LumiCube``: ``display``, ``microphone``,
    ``speaker``, ``screen``, ``buttons``, ``light_sensor``, ``imu``,
    ``env_sensor``, ``pi``. Only ``display`` is hardware-backed for now;
    the rest are warn+no-op stubs (see [README.md] Todo list).
    """

    def __init__(self, cube: LumiCube) -> None:
        self._cube = cube
        self.display = DisplayShim(cube.display)
        self.buttons = StubModule(
            "buttons",
            available_fields=(
                "top_pressed", "middle_pressed", "bottom_pressed",
                "top_pressed_count", "middle_pressed_count", "bottom_pressed_count",
            ),
        )
        self.light_sensor = StubModule(
            "light_sensor",
            available_fields=(
                "ambient_light", "red", "green", "blue", "last_gesture",
                "num_gestures", "within_proximity", "num_times_within_proximity",
            ),
        )
        self.env_sensor = StubModule(
            "env_sensor", available_fields=("temperature", "pressure", "humidity"),
        )
        self.imu = StubModule(
            "imu",
            available_fields=(
                "pitch", "roll", "yaw",
                "acceleration_x", "acceleration_y", "acceleration_z",
                "angular_velocity_x", "angular_velocity_y", "angular_velocity_z",
                "gravity_x", "gravity_y", "gravity_z",
            ),
        )
        self.screen = StubModule("screen")
        self.speaker = StubModule("speaker", available_fields=("volume",))
        self.microphone = StubModule("microphone", available_fields=("enable",))
        self.pi = StubModule("pi")


# ----- Globals builder + runner -------------------------------------------


def build_globals(cube: LumiCube | LumiCubeCompat) -> dict[str, Any]:
    """Construct the namespace dict that mirrors what foundry-daemon
    injects into a script before ``exec()``ing it.
    """
    compat = cube if isinstance(cube, LumiCubeCompat) else LumiCubeCompat(cube)
    ns: dict[str, Any] = {
        # Pre-imported standard library modules — upstream does the same.
        "time": time,
        "math": math,
        "random": random,
        # Hardware facade + per-module top-level aliases.
        "cube": compat,
        "display": compat.display,
        "buttons": compat.buttons,
        "light_sensor": compat.light_sensor,
        "env_sensor": compat.env_sensor,
        "imu": compat.imu,
        "screen": compat.screen,
        "speaker": compat.speaker,
        "microphone": compat.microphone,
        "pi": compat.pi,
        # Colour constants (both spellings — upstream `api.txt` uses
        # American "color" elsewhere, but the canonical ones are British).
        "black": BLACK,
        "grey": GREY,
        "white": WHITE,
        "red": RED,
        "orange": ORANGE,
        "yellow": YELLOW,
        "green": GREEN,
        "cyan": CYAN,
        "blue": BLUE,
        "magenta": MAGENTA,
        "pink": PINK,
        "purple": PURPLE,
        # Helpers.
        "hsv_colour": hsv_colour,
        "random_colour": random_colour,
        "noise_2d": noise_2d,
        "noise_3d": noise_3d,
        "noise_4d": noise_4d,
        "run_async": run_async,
        # Speaker waveform sentinels.
        "sine_wave": sine_wave,
        "square_wave": square_wave,
        "white_noise": white_noise,
        # Class for "another cube" usage; we don't yet support a remote
        # IP-addressed cube, so calling with an address falls through to
        # the local serial port.
        "LumiCube": LumiCubeCompat,
        "Cube": LumiCubeCompat,
        # Module file metadata so traceback shows the user's script name.
        "__name__": "__main__",
    }
    return ns


def run_script(
    path: str,
    *,
    cube: LumiCube | None = None,
    port: str = SERIAL_DEVICE,
    extra_globals: Mapping[str, Any] | None = None,
) -> None:
    """Run ``path`` as if loaded by the foundry-daemon script host.

    If ``cube`` is None, a `LumiCube` is opened on ``port`` for the
    duration of the script and closed cleanly on exit.
    """
    with open(path, "rb") as fh:
        source = fh.read()
    code = compile(source, path, "exec")

    own_cube = cube is None
    if own_cube:
        cube = LumiCube(port=port)
        cube.start()
    try:
        assert cube is not None
        ns = build_globals(cube)
        ns["__file__"] = path
        if extra_globals:
            ns.update(extra_globals)
        # Inject the script's directory into sys.path so relative
        # imports/file references (e.g. chime files) work the same way
        # they did under foundry-daemon.
        import os
        script_dir = os.path.dirname(os.path.abspath(path)) or "."
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        exec(code, ns)
    finally:
        if own_cube and cube is not None:
            cube.stop()
