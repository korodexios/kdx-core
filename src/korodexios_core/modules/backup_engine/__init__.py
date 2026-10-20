"""backup_engine: source-to-destination backups with validation and dry runs."""

from __future__ import annotations

from .engine import (
    BackupError,
    BackupPlan,
    BackupResult,
    BackupSettings,
    build_plan,
    execute_plan,
)
from .module import COMPLETED_EVENT, CONFIGURED_EVENT, MODULE, BackupEngineModule

__all__ = [
    "COMPLETED_EVENT",
    "CONFIGURED_EVENT",
    "MODULE",
    "BackupEngineModule",
    "BackupError",
    "BackupPlan",
    "BackupResult",
    "BackupSettings",
    "build_plan",
    "execute_plan",
]
