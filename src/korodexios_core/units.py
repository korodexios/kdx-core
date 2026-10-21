"""Small formatting and platform helpers shared by modules and the CLI.

No third-party imports: the chassis keeps its runtime to PyYAML and rich.
"""

from __future__ import annotations

import os

__all__ = ["DEFAULT_PAGE_SIZE", "format_bytes", "format_duration", "page_size", "sysconf_value"]

_BYTE_UNITS = ("bytes", "KB", "MB", "GB", "TB", "PB", "EB")


def format_bytes(value: int | float | None) -> str:
    """Return a human-readable size such as '12.3 MB' (binary steps)."""
    if value is None or value < 0:
        return "-"
    size = float(value)
    for unit in _BYTE_UNITS:
        if size < 1_024 or unit == _BYTE_UNITS[-1]:
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1_024
    return f"{size:.1f} EB"


def format_duration(seconds: float | None) -> str:
    """Return a compact duration such as '1.2s', '4m 30s', '2h 5m'."""
    if seconds is None or seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


#: Fallback for platforms without POSIX sysconf, such as Windows.
DEFAULT_PAGE_SIZE = 4096


def sysconf_value(name: str) -> int | None:
    """Return a POSIX sysconf value, or None where the platform lacks sysconf."""
    sysconf = getattr(os, "sysconf", None)
    if sysconf is None:
        return None
    try:
        value = int(sysconf(name))
    except (ValueError, OSError):
        return None
    return value if value > 0 else None


def page_size() -> int:
    """Return the OS page size, falling back to the 4 KiB default."""
    return sysconf_value("SC_PAGE_SIZE") or DEFAULT_PAGE_SIZE
