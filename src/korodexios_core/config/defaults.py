"""Built-in defaults: the lowest layer of the configuration hierarchy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

CORE_DEFAULTS: Final[dict[str, Any]] = {
    "core.name": "korodexios",
    "core.distribution": "korodexios-core",
    "core.home": None,
    "core.log.level": "INFO",
    "core.log.dir": "var/log",
    "core.log.file": "korodex.log",
    "core.log.file_enabled": True,
    "core.log.json": False,
    "core.log.color": True,
    "core.log.utc": False,
    "core.log.max_bytes": 1_048_576,
    "core.log.backup_count": 5,
    "core.modules.autodiscover": True,
    "core.modules.enabled": [],
    "core.modules.disabled": [],
    "core.modules.extra_paths": [],
    "core.lifecycle.tick_interval": 1.0,
    "core.lifecycle.stop_timeout": 5.0,
    "core.cli.color": True,
    "core.cli.interactive": True,
    "core.cli.confirmations": True,
    "core.cli.width": 100,
    "core.env.prefix": "KORODEX",
}

RUNTIME_DEFAULT_KEYS: Final[tuple[str, ...]] = ("core.home",)


def default_for(key: str) -> Any:
    """Return the built-in default for 'key', or None when the key is unknown."""
    return CORE_DEFAULTS.get(key)


def with_runtime_defaults(values: Mapping[str, Any]) -> dict[str, Any]:
    """Merge discovered runtime values into a copy of the built-in defaults."""
    merged = dict(CORE_DEFAULTS)
    merged.update({key: value for key, value in values.items() if key in RUNTIME_DEFAULT_KEYS})
    return merged
