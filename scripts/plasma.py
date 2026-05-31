# credit: ported from the Abstract Foundry community script `lava_lamp.py`.
"""Plasma animation for the LumiCube — native `pylumicube` API.

3D plasma effect (a lava-lamp / flowing-hue field): each of the 192
surface pixels samples 4D OpenSimplex noise at
``(x*scale, y*scale, z*scale, t*speed)``; the result is mapped to an HSV
hue at full saturation/brightness and pushed to the cube.

Differences vs. the upstream community-script version (which runs under
`lumicube-run`'s compat shim) that make this version meaningfully
faster on a Pi:

  * **Surface geometry is precomputed.** The 192 LED-bearing
    coordinates of the 9×9×9 cube and their LED indices are built once
    at startup; the frame loop never touches the 9×9×9 = 729 inner
    triple-loop or the surface-membership check.
  * **HSV → packed RGB is vectorised in numpy.** A single ``np.choose``
    over 192 hues beats 192 individual ``colorsys.hsv_to_rgb`` calls.
  * **Display writes are async** (the default on `Display.set_leds`),
    so the next frame's noise computation overlaps the previous
    frame's SET_FIELDS push over the wire.

Note: 4D OpenSimplex noise itself is pure-Python in this library
(`opensimplex>=0.4`) and dominates frame time (~90%). A scalar loop is
faster than `noise4array` without numba installed, so we stay with
scalar `noise4()` calls. Installing `numba` would JIT the inner noise
function and give a much bigger win — out of scope for the
`[extras]` install group for now.

Run as a plain Python script:

    uv run python scripts/plasma.py
    # or, in an activated venv:
    python scripts/plasma.py

Also works under `lumicube-run` — `open_or_use_hosted` picks up the
runner's already-open cube:

    uv run lumicube-run scripts/plasma.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import numpy as np
import opensimplex

from pylumicube.compat import get_hosted_cube, open_or_use_hosted
from pylumicube.constants import SERIAL_DEVICE

log = logging.getLogger("plasma")


# ----- Tuning knobs (match upstream `lava_lamp.py` defaults) -----
# (kept under the original community-script's defaults to preserve the
# look; rename was purely cosmetic.)

SCALE = 0.10
SPEED = 0.05
TARGET_FPS = 30
NOISE_SEED = 123456


# --------------------------------------------------------------------------
# Geometry — 192 surface pixels of the 9×9×9 cube.
#
# A coordinate is on the LED surface when exactly one of x, y, z is 8
# (the panel-far edge in the upstream coordinate system). The LED-index
# formulae are pinned to `pylumicube.compat.runtime._xyz_to_index` (and
# tested against the upstream daemon in test_compat_mappings.py).
# --------------------------------------------------------------------------


def _xyz_to_index(x: int, y: int, z: int) -> int | None:
    if x < 8 and y < 8 and z == 8:
        return 63 - x - 8 * y
    if y < 8 and z < 8 and x == 8:
        return 127 - y - 8 * z
    if z < 8 and x < 8 and y == 8:
        return 191 - z - 8 * x
    return None


def _build_surface_points() -> tuple[list[tuple[float, float, float]], list[int]]:
    """Return (scaled_coords, led_indices) for the 192 surface pixels.

    ``scaled_coords`` is a list of (x*SCALE, y*SCALE, z*SCALE) tuples in
    the same order as ``led_indices``; both are length-192. Built once
    at startup; the frame loop just walks ``scaled_coords``.
    """
    scaled: list[tuple[float, float, float]] = []
    leds: list[int] = []
    for x in range(9):
        for y in range(9):
            for z in range(9):
                idx = _xyz_to_index(x, y, z)
                if idx is None:
                    continue
                scaled.append((x * SCALE, y * SCALE, z * SCALE))
                leds.append(idx)
    return scaled, leds


# --------------------------------------------------------------------------
# Vectorised HSV → 0xRRGGBB packer (sat = 1, val = 1).
#
# Standard HSV-to-RGB formula simplified for S=V=1: each sector of the
# hue circle picks 3 of {0, 1, t, q} as the RGB triplet, where
# t = h*6 - floor(h*6) and q = 1 - t. We compute one numpy array per
# channel and pack into uint32 0xRRGGBB.
# --------------------------------------------------------------------------


def _hues_to_packed_rgb(hues: np.ndarray) -> np.ndarray:
    h = np.mod(hues, 1.0)
    h6 = h * 6.0
    sector = h6.astype(np.int32) % 6
    t = h6 - h6.astype(np.int32)
    q = 1.0 - t
    zero = np.zeros_like(h)
    one = np.ones_like(h)
    r = np.choose(sector, [one,  q,    zero, zero, t,    one])
    g = np.choose(sector, [t,    one,  one,  q,    zero, zero])
    b = np.choose(sector, [zero, zero, t,    one,  one,  q])
    R = (np.clip(r, 0.0, 1.0) * 255).astype(np.uint32)
    G = (np.clip(g, 0.0, 1.0) * 255).astype(np.uint32)
    B = (np.clip(b, 0.0, 1.0) * 255).astype(np.uint32)
    return (R << 16) | (G << 8) | B


# --------------------------------------------------------------------------
# Main loop.
# --------------------------------------------------------------------------


def run(port: str = SERIAL_DEVICE) -> None:
    scaled_coords, led_indices = _build_surface_points()

    # `open_or_use_hosted` yields lumicube-run's already-open cube when
    # the script is invoked via that runner, and opens a fresh `LumiCube`
    # otherwise. Either way the context manager handles teardown.
    with open_or_use_hosted(port) as cube:
        display = cube.display
        display.fill(0x000000, await_ack=True)   # blank synchronously up front

        gen = opensimplex.OpenSimplex(seed=NOISE_SEED)
        noise4 = gen.noise4   # local binding shaves attribute-lookup time per call
        period = 1.0 / TARGET_FPS

        log.info(
            "running (target %d fps, %d surface pixels, scale=%g, speed=%g)",
            TARGET_FPS, len(led_indices), SCALE, SPEED,
        )

        t = 0
        while True:
            frame_start = time.monotonic()
            w = SPEED * t

            # Scalar noise per surface point — fastest path through this
            # opensimplex version. `fromiter` collects into a numpy array
            # without going through a Python list first.
            hues = np.fromiter(
                (noise4(x, y, z, w) for (x, y, z) in scaled_coords),
                dtype=np.float64, count=192,
            )
            rgbs = _hues_to_packed_rgb(hues)
            display.set_leds(dict(zip(led_indices, rgbs.tolist())))  # async

            t += 1
            dt = time.monotonic() - frame_start
            if dt < period:
                time.sleep(period - dt)


def main(argv: list[str] | None = None) -> int:
    # Hosted-mode short-circuit (same pattern as scripts/digital_clock.py):
    # when exec'd under `lumicube-run`, the runner has already opened the
    # cube and parsed its own CLI flags; the contents of sys.argv would
    # confuse our argparse, so skip it entirely.
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

    parser = argparse.ArgumentParser(description="LumiCube plasma (native pylumicube API)")
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
