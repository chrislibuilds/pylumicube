"""Compatibility shim for upstream Abstract Foundry community scripts.

The upstream foundry-daemon hands a script a pre-populated globals dict
containing `cube`, `display`, colour constants, `hsv_colour`, `noise_*`,
etc., then ``exec``s the script. This package recreates that environment
on top of pylumicube so existing community scripts run unchanged.

Entry points:

    from pylumicube.compat import build_globals, run_script

    with LumiCube() as cube:
        ns = build_globals(cube)
        # ...or...
        run_script('scripts/binary_clock.py', cube=cube)
"""

from .runtime import (
    LumiCubeCompat,
    build_globals,
    get_hosted_cube,
    open_or_use_hosted,
    run_script,
)

__all__ = [
    "LumiCubeCompat",
    "build_globals",
    "get_hosted_cube",
    "open_or_use_hosted",
    "run_script",
]
