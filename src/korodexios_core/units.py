"""Small formatting helpers shared by modules and the CLI (no third-party imports)."""

from __future__ import annotations

__all__ = ["format_bytes", "format_duration"]

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
