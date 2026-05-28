# credit to @arustleund#0019 (Discord) for the original script
"""Digital clock for the LumiCube — drives the native `pylumicube` API.

Renders the current hour (left panel) and minute (right panel), plus a
filling-dot seconds animation on the top panel. Optionally fetches the
current temperature from openweathermap.org and shows it for a few
seconds at the top of each minute.

Unlike the upstream community scripts in this directory, this one does
not need ``lumicube-run`` — it opens the cube via `pylumicube.LumiCube`
directly. Run it as a normal Python script:

    uv run python scripts/digital_clock.py
    # or, in an activated venv:
    python scripts/digital_clock.py

Configuration lives in ``scripts/digital_clock_config.py``. Copy the
checked-in ``digital_clock_config.py.example`` to that path and edit
the OpenWeatherMap API key + city ID to enable the weather feature.
The script runs without the config file (with weather disabled).

Original credit: @arustleund#0019 (Discord) for the upstream
``fancyclock.py`` this was ported from.
"""

from __future__ import annotations

import argparse
import colorsys
import datetime
import logging
import os
import random
import sys
import time
from typing import Optional, TypeVar

from pylumicube.compat import get_hosted_cube, open_or_use_hosted
from pylumicube.constants import SERIAL_DEVICE

log = logging.getLogger("digital_clock")


# --------------------------------------------------------------------------
# Config loading — try `digital_clock_config.py` sitting next to this file;
# if it's missing, fall back to the defaults below (weather disabled).
# --------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import digital_clock_config as _cfg  # type: ignore[import-not-found]
except ImportError:
    _cfg = None


_T = TypeVar("_T")


def _cfg_get(name: str, default: _T) -> _T:
    if _cfg is None:
        return default
    return getattr(_cfg, name, default)


OPENWEATHERMAP_API_KEY = _cfg_get("OPENWEATHERMAP_API_KEY", "")
OPENWEATHERMAP_CITY_ID = _cfg_get("OPENWEATHERMAP_CITY_ID", "2858738")
USE_24_HOUR_CLOCK = _cfg_get("USE_24_HOUR_CLOCK", True)
USE_RANDOM_HOUR_COLORS = _cfg_get("USE_RANDOM_HOUR_COLORS", True)
USE_RANDOM_MINUTE_COLORS = _cfg_get("USE_RANDOM_MINUTE_COLORS", True)
USE_RANDOM_DOTS_FOR_SECONDS = _cfg_get("USE_RANDOM_DOTS_FOR_SECONDS", True)
HOUR_COLOR_HUE = _cfg_get("HOUR_COLOR_HUE", 0.8)
HOUR_COLOR_SATURATION = _cfg_get("HOUR_COLOR_SATURATION", 1.0)
MINUTE_COLOR_HUE = _cfg_get("MINUTE_COLOR_HUE", 0.0)
MINUTE_COLOR_SATURATION = _cfg_get("MINUTE_COLOR_SATURATION", 0.0)
SECOND_COLOR_HUE = _cfg_get("SECOND_COLOR_HUE", 0.6)
SECOND_COLOR_SATURATION = _cfg_get("SECOND_COLOR_SATURATION", 0.8)
BRIGHTNESS = _cfg_get("BRIGHTNESS", 0.7)
REFRESH_RATE = _cfg_get("REFRESH_RATE", 1 / 20)
WEATHER_REFRESH_SECONDS = _cfg_get("WEATHER_REFRESH_SECONDS", 600)
WEATHER_SHOW_SECONDS = _cfg_get("WEATHER_SHOW_SECONDS", 5)

# `requests` is an optional install — pulled in via the `[extras]` extra.
try:
    import requests  # type: ignore[import-not-found]
except ImportError:
    requests = None  # type: ignore[assignment]

WEATHER_ENABLED = bool(OPENWEATHERMAP_API_KEY) and requests is not None
WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"


# --------------------------------------------------------------------------
# Colour helpers and the (x, y) → LED index mapping.
#
# The native `LumiCube.display.set_leds` API takes a flat ``{0..191: RGB}``
# dict. The mapping below converts the upstream daemon's 16×16 logical
# coordinate system to those indices, matching the formula used by
# pylumicube's compat shim (and the upstream daemon's
# `foundry_api/standard_library.py`).
# --------------------------------------------------------------------------


def hsv_colour(hue: float, sat: float, val: float) -> int:
    """HSV (each 0..1) → packed 24-bit 0xRRGGBB int."""
    hue = hue % 1
    sat = max(0.0, min(float(sat), 1.0))
    val = max(0.0, min(float(val), 1.0))
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def random_color_hue_sat() -> tuple[float, float]:
    return random.randint(0, 100) / 100, random.randint(0, 100) / 100


def xy_to_index(x: int, y: int) -> Optional[int]:
    if x < 0 or y < 0:
        return None
    if x < 8 and y < 8:
        return 63 - x - 8 * y
    if x < 16 and y < 8:
        return 7 - y + 8 * x
    if x < 8 and y < 16:
        return 176 + y - 8 * x
    return None


def push_xy_leds(display, xy_to_colour: dict[tuple[int, int], int]) -> None:
    """Convert {(x, y): rgb} → {index: rgb} and call the native set_leds."""
    indexed: dict[int, int] = {}
    for key, colour in xy_to_colour.items():
        idx = xy_to_index(*key)
        if idx is not None:
            indexed[idx] = colour & 0xFFFFFF
    if indexed:
        display.set_leds(indexed, show=True)


# --------------------------------------------------------------------------
# Tall digit font (3 cols × 8 rows per glyph, stored column-major).
# Original credit: @arustleund#0019.
# --------------------------------------------------------------------------


TALL_FONT = [
    0b1011111111, 0b1111011111, 0b1011111111,
    0b1100111011, 0b0100000000, 0b1011100111,
    0b1000111011, 0b0100000000, 0b1011100111,
    0b1011111011, 0b0111111011, 0b1011111111,
    0b1010001010, 0b0100000000, 0b1001111111,
    0b1010001010, 0b0100000000, 0b1001111111,
    0b1010001010, 0b0100000000, 0b1001111111,
    0b1111011011, 0b1111011011, 0b1111111111,
]


def draw_number(leds: dict, number: int, x_offset: int, y_offset: int, color: int) -> None:
    mask = pow(2, 9 - number)
    for y in range(8):
        for x in range(3):
            tf_index = (21 - (y * 3)) + (x % 3)
            tf_value = TALL_FONT[tf_index]
            bit_on = bool(mask & tf_value)
            leds[(x + x_offset, y + y_offset)] = color if bit_on else 0


def draw_double_digit_number(leds: dict, number: int, x_offset: int, y_offset: int, color: int) -> None:
    draw_number(leds, number // 10, x_offset, y_offset, color)
    draw_number(leds, number % 10, x_offset + 4, y_offset, color)


# --------------------------------------------------------------------------
# Optional weather fetch.
# --------------------------------------------------------------------------


def fetch_temperature_celsius() -> Optional[int]:
    if not WEATHER_ENABLED or requests is None:
        return None
    params = {
        "appid": OPENWEATHERMAP_API_KEY,
        "id": OPENWEATHERMAP_CITY_ID,
        "units": "metric",
    }
    try:
        response = requests.get(WEATHER_URL, params=params, timeout=5)
        response.raise_for_status()
        return int(response.json()["main"]["temp"])
    except Exception as exc:  # noqa: BLE001
        log.warning("weather fetch failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# Main loop.
# --------------------------------------------------------------------------


def run(port: str = SERIAL_DEVICE) -> None:
    global HOUR_COLOR_HUE, HOUR_COLOR_SATURATION
    global MINUTE_COLOR_HUE, MINUTE_COLOR_SATURATION

    # `open_or_use_hosted` yields lumicube-run's already-open cube when
    # the script is invoked via that runner, and opens a fresh `LumiCube`
    # otherwise. Either way the context manager handles teardown.
    with open_or_use_hosted(port) as cube:
        display = cube.display
        display.fill(0x000000)

        rand_indexes = list(range(64))
        random.shuffle(rand_indexes)
        seconds_palette = [(SECOND_COLOR_HUE, SECOND_COLOR_SATURATION)] * 64

        time_now = datetime.datetime.now()
        recent_hour = (time_now.hour - 1, time_now.hour, time_now.hour - 12)
        recent_minute = time_now.minute - 1

        last_weather_fetch = 0.0
        temperature: Optional[int] = None

        log.info(
            "running (weather %s, refresh every %ss)",
            "ENABLED" if WEATHER_ENABLED else "disabled",
            REFRESH_RATE,
        )

        while True:
            leds: dict[tuple[int, int], int] = {(x, y): 0 for x in range(16) for y in range(16)}
            time_now = datetime.datetime.now()

            # Hours (left panel) ----------------------------------------------------------
            latest_hour = (
                time_now.hour
                if USE_24_HOUR_CLOCK
                else ((time_now.hour - 1) % 12 + 1)
            )
            if latest_hour not in recent_hour:
                if USE_RANDOM_HOUR_COLORS:
                    HOUR_COLOR_HUE, HOUR_COLOR_SATURATION = random_color_hue_sat()
                recent_hour = (latest_hour, latest_hour - 12)
            draw_double_digit_number(
                leds, latest_hour, 1, 0,
                hsv_colour(HOUR_COLOR_HUE, HOUR_COLOR_SATURATION, BRIGHTNESS),
            )

            # Minutes (right panel) -------------------------------------------------------
            if time_now.minute != recent_minute:
                if USE_RANDOM_MINUTE_COLORS:
                    MINUTE_COLOR_HUE, MINUTE_COLOR_SATURATION = random_color_hue_sat()
                recent_minute = time_now.minute
            draw_double_digit_number(
                leds, time_now.minute, 9, 0,
                hsv_colour(MINUTE_COLOR_HUE, MINUTE_COLOR_SATURATION, BRIGHTNESS),
            )

            # Weather refresh -------------------------------------------------------------
            if WEATHER_ENABLED:
                now_mono = time.monotonic()
                if now_mono - last_weather_fetch > WEATHER_REFRESH_SECONDS:
                    temperature = fetch_temperature_celsius()
                    last_weather_fetch = now_mono

            # Top panel: temperature for the first N seconds of each minute, otherwise
            # the seconds animation.
            temp_int = temperature if temperature is not None else -1
            show_weather = (
                WEATHER_ENABLED
                and 0 <= temp_int < 100
                and time_now.second < WEATHER_SHOW_SECONDS
            )
            if show_weather:
                draw_double_digit_number(
                    leds, temp_int, 1, 8,
                    hsv_colour(SECOND_COLOR_HUE, SECOND_COLOR_SATURATION, BRIGHTNESS),
                )
            elif not USE_RANDOM_DOTS_FOR_SECONDS:
                draw_double_digit_number(
                    leds, time_now.second, 1, 8,
                    hsv_colour(SECOND_COLOR_HUE, SECOND_COLOR_SATURATION, BRIGHTNESS),
                )
            else:
                percent_seconds = (
                    (time_now.second * 1_000_000) + time_now.microsecond
                ) / 60_000_000
                how_lit = percent_seconds * 64
                for y in range(8):
                    for x in range(8):
                        led_index = (y * 8) + x
                        hue, saturation = seconds_palette[led_index]
                        if how_lit >= led_index:
                            diff = how_lit - led_index
                            plot_x = rand_indexes[led_index] % 8
                            plot_y = rand_indexes[led_index] // 8
                            percent_lit = min(1.0, diff)
                            leds[(plot_x, plot_y + 8)] = hsv_colour(
                                hue, saturation, BRIGHTNESS * percent_lit,
                            )

            push_xy_leds(display, leds)
            time.sleep(REFRESH_RATE)


def main(argv: list[str] | None = None) -> int:
    # If we were exec'd under `lumicube-run`, the runner has already opened
    # the cube *and* parsed its own CLI flags — the contents of sys.argv
    # would confuse our own argparse here. Detect that case (a registered
    # hosted cube is the unambiguous signal) and skip argparse entirely.
    if get_hosted_cube() is not None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        try:
            run()
        except KeyboardInterrupt:
            print("\nstopped")
        return 0

    parser = argparse.ArgumentParser(description="LumiCube digital clock (native pylumicube API)")
    parser.add_argument("--port", default=SERIAL_DEVICE,
                        help=f"serial device (default {SERIAL_DEVICE})")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run(port=args.port)
    except KeyboardInterrupt:
        print("\nstopped")
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
