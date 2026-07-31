"""Logging setup.

The library never configures logging on import: only the CLI calls
:func:`configure_logging`. Importing this package must not hijack a host
application's log configuration.
"""

from __future__ import annotations

import sys
from typing import Final

from loguru import logger

__all__ = ["configure_logging", "logger"]

_FORMAT: Final = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
)
_COMPACT_FORMAT: Final = "<level>{level: <8}</level> | <level>{message}</level>"


def configure_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Install a single stderr sink.

    Args:
        verbose: Emit DEBUG records with module and function names.
        quiet: Emit WARNING and above only. Wins over ``verbose``.
    """
    logger.remove()
    if quiet:
        level, fmt = "WARNING", _COMPACT_FORMAT
    elif verbose:
        level, fmt = "DEBUG", _FORMAT
    else:
        level, fmt = "INFO", _COMPACT_FORMAT
    logger.add(sys.stderr, level=level, format=fmt, backtrace=verbose, diagnose=False)
