"""pylumicube — Pure-Python driver for the Abstract Foundry LumiCube.

See PROTOCOL.md for the wire format and README.md for usage.
"""

from importlib.metadata import PackageNotFoundError, version

from .node import LumiCube
from .display import Display

try:
    # Single source of truth is the [project] version in pyproject.toml,
    # read here from the installed distribution metadata.
    __version__ = version("pylumicube")
except PackageNotFoundError:  # not installed (e.g. running from a bare checkout)
    __version__ = "0.0.0+unknown"

__all__ = ["LumiCube", "Display", "__version__"]
