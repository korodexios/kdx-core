"""The context handed to every module: config, logger, event bus, paths, registry."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.layered import LayeredConfig, ScopedConfig
from ..events.bus import EventBus
from ..logging.engine import KorodexiosLogger, LoggerFactory
from ..paths import AppPaths
from ..version import CORE_API_VERSION, __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import ModuleRegistry


@dataclass(slots=True)
class CoreContext:
    """Everything a module may legitimately reach for."""

    config: LayeredConfig
    paths: AppPaths
    logs: LoggerFactory
    bus: EventBus
    app_name: str = "korodexios"
    app_version: str = __version__
    core_api: str = CORE_API_VERSION
    started_at: float = field(default_factory=time.time)
    registry: ModuleRegistry | None = None
    chassis: Any = None

    def logger_for(self, name: str) -> KorodexiosLogger:
        """Return a logger namespaced below the chassis namespace."""
        return self.logs.get(name)

    def module_config(self, name: str) -> ScopedConfig:
        """Return the configuration subtree for module *name*."""
        return self.config.child(f"modules.{name}")

    def module_dir(self, name: str) -> Path:
        """Return (and create) the private data directory for module *name*."""
        return self.paths.module_dir(name)

    def uptime(self) -> float:
        """Return seconds since the chassis started."""
        return max(0.0, time.time() - self.started_at)

    def publish(self, name: str, payload: Any = None, *, source: str | None = None) -> int:
        """Publish an event on the shared bus."""
        return self.bus.publish(name, payload, source=source)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the context."""
        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "core_api": self.core_api,
            "uptime_seconds": round(self.uptime(), 3),
            "config_file": str(self.config.config_file) if self.config.config_file else None,
            "env_file": str(self.config.env_file) if self.config.env_file else None,
            "paths": self.paths.as_dict(),
            "logging": self.logs.stats(),
            "bus": self.bus.stats(),
        }

    def __repr__(self) -> str:
        return (
            f"CoreContext(app={self.app_name!r}, version={self.app_version!r}, "
            f"config_file={self.config.config_file!s})"
        )
