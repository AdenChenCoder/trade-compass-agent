"""Trade Compass Agent package."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("trade-compass-agent")
except PackageNotFoundError:
    __version__ = "0.2.0rc3"
