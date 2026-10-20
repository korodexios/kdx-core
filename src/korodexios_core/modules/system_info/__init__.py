"""system_info: host OS, CPU, RAM, and disk diagnostics built into the chassis."""

from __future__ import annotations

from .module import MODULE, SNAPSHOT_EVENT, SystemInfoModule

__all__ = ["MODULE", "SNAPSHOT_EVENT", "SystemInfoModule"]
