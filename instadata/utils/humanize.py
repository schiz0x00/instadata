"""Human-readable sizes, rates and durations for log output.

Binary units throughout (KiB, MiB), because that is what the rest of the
program already reports and mixing the two in one log is worse than either.
"""

from __future__ import annotations

__all__ = ["format_bytes", "format_duration", "format_rate"]

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(count: float) -> str:
    """Render a byte count in the largest unit that keeps it readable.

    Bytes stay integral; everything larger gets one decimal, which is enough
    precision to watch a transfer without the number jittering every line.

    >>> format_bytes(512)
    '512 B'
    >>> format_bytes(1536)
    '1.5 KiB'
    """
    value = float(count)
    for unit in _UNITS:
        if abs(value) < 1024.0 or unit == _UNITS[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    raise AssertionError("unreachable")  # pragma: no cover


def format_rate(bytes_per_second: float) -> str:
    """Render a transfer rate.

    >>> format_rate(1536)
    '1.5 KiB/s'
    """
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float) -> str:
    """Render a short duration.

    >>> format_duration(0.42)
    '0.4s'
    >>> format_duration(95)
    '1m35s'
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
