"""Host inspection with the standard library only: OS, CPU, memory, disk, uptime."""

from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from ...units import format_bytes as core_format_bytes
from ...units import page_size, sysconf_value

__all__ = [
    "cpu_snapshot",
    "disk_snapshot",
    "format_bytes",
    "full_snapshot",
    "load_reading",
    "memory_snapshot",
    "os_snapshot",
    "supports_load_average",
    "uptime_snapshot",
]

_PROC_MEMINFO = Path("/proc/meminfo")
_PROC_UPTIME = Path("/proc/uptime")
_PROC_CPUINFO = Path("/proc/cpuinfo")


def format_bytes(value: int | float | None) -> str:
    """Return a human-readable size such as '12.3 MB'."""
    return core_format_bytes(value)


def os_snapshot() -> dict[str, Any]:
    """Return operating-system identity and interpreter facts."""
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "encoding": (getattr(getattr(__import__("sys"), "stdout", None), "encoding", None) or "-"),
    }


def load_reading() -> list[float] | None:
    """Return the one, five, and fifteen minute load average, or None.

    The load average is a Unix idea: a platform without `os.getloadavg()`, or one that
    refuses the reading, answers None and the caller decides what that means.
    """
    if not hasattr(os, "getloadavg"):
        return None
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def supports_load_average() -> bool:
    """Return whether this platform can answer load_reading() at all."""
    return hasattr(os, "getloadavg")


def cpu_snapshot(*, include_model: bool = True) -> dict[str, Any]:
    """Return CPU count, load average, and (on Linux) the model name."""
    load = load_reading()
    return {
        "logical_cores": os.cpu_count(),
        "load_average": load,
        "architecture": platform.machine(),
        "processor": _cpu_model() if include_model else None,
    }


def memory_snapshot() -> dict[str, Any]:
    """Return total/available/used memory, preferring /proc/meminfo on Linux."""
    total: int | None = None
    available: int | None = None
    source = "unavailable"
    meminfo = _meminfo_values()
    if meminfo:
        total = meminfo.get("MemTotal")
        available = meminfo.get("MemAvailable", meminfo.get("MemFree"))
        source = "proc/meminfo"
    if total is None:
        pages = sysconf_value("SC_PHYS_PAGES")
        if pages is not None:
            total = page_size() * pages
            source = "sysconf"
    used = max(0, total - available) if total is not None and available is not None else None
    percent = round(100.0 * used / total, 1) if total and used is not None else None
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "used_percent": percent,
        "source": source,
        "total": format_bytes(total),
        "available": format_bytes(available),
        "used": format_bytes(used),
    }


def disk_snapshot(target: str | Path = ".") -> dict[str, Any]:
    """Return disk usage for the filesystem holding *target*."""
    resolved = Path(str(target)).expanduser()
    probe = resolved if resolved.exists() else Path.cwd()
    usage = shutil.disk_usage(str(probe))
    stats: dict[str, Any] = {}
    if hasattr(os, "statvfs"):
        try:
            raw = os.statvfs(str(probe))
            stats = {
                "inodes_total": raw.f_files,
                "inodes_free": raw.f_ffree,
                "block_size": raw.f_bsize,
            }
        except OSError:
            stats = {}
    percent = round(100.0 * usage.used / usage.total, 1) if usage.total else None
    return {
        "path": str(probe),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": percent,
        "total": format_bytes(usage.total),
        "used": format_bytes(usage.used),
        "free": format_bytes(usage.free),
        **stats,
    }


def uptime_snapshot() -> dict[str, Any]:
    """Return host uptime when the platform exposes it."""
    result: dict[str, Any] = {"seconds": None, "pretty": "-", "source": "unavailable"}
    if _PROC_UPTIME.is_file():
        try:
            seconds = float(_PROC_UPTIME.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return result
        result.update(seconds=round(seconds, 1), pretty=_pretty(seconds), source="proc/uptime")
    return result


def full_snapshot(root: str | Path = ".", *, include_model: bool = True) -> dict[str, Any]:
    """Return every section in one JSON-friendly mapping."""
    uptime = uptime_snapshot()
    return {
        "timestamp": time.time(),
        "os": os_snapshot(),
        "cpu": cpu_snapshot(include_model=include_model),
        "memory": memory_snapshot(),
        "disk": disk_snapshot(root),
        "uptime": uptime,
    }


def _meminfo_values() -> dict[str, int]:
    if not _PROC_MEMINFO.is_file():
        return {}
    values: dict[str, int] = {}
    try:
        text = _PROC_MEMINFO.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) >= 1 and parts[0].isdigit():
            unit = parts[1].lower() if len(parts) > 1 else "kb"
            multiplier = 1_024 if unit in ("kb", "kib") else 1
            values[key.strip()] = int(parts[0]) * multiplier
    return values


def _cpu_model() -> str | None:
    if not _PROC_CPUINFO.is_file():
        return platform.processor() or None
    try:
        text = _PROC_CPUINFO.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "model name":
            return value.strip()
    return None


def _pretty(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)
