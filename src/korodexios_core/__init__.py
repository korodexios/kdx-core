"""korodexios-core: a headless microkernel chassis for Korodexios projects."""

from __future__ import annotations

from .config import CORE_DEFAULTS, ConfigSource, LayeredConfig, ScopedConfig
from .constants import TAG_ASK, TAG_ERROR, TAG_NOTE, TAG_OK, ExitCode
from .events import Event, EventBus, Subscription
from .exceptions import (
    ChassisError,
    CliError,
    ConfigFileError,
    ConfigurationError,
    ConfigValueError,
    KorodexError,
    ModuleContractError,
    ModuleDiscoveryError,
    ModuleLifecycleError,
    PathDiscoveryError,
)
from .logging import KorodexLogger, LoggerFactory, build_logger
from .paths import AppPaths
from .version import CORE_API_VERSION, __version__

__all__ = [
    "CORE_API_VERSION",
    "CORE_DEFAULTS",
    "TAG_ASK",
    "TAG_ERROR",
    "TAG_NOTE",
    "TAG_OK",
    "AppPaths",
    "ChassisError",
    "CliError",
    "ConfigFileError",
    "ConfigSource",
    "ConfigValueError",
    "ConfigurationError",
    "Event",
    "EventBus",
    "ExitCode",
    "KorodexError",
    "KorodexLogger",
    "LayeredConfig",
    "LoggerFactory",
    "ModuleContractError",
    "ModuleDiscoveryError",
    "ModuleLifecycleError",
    "PathDiscoveryError",
    "ScopedConfig",
    "Subscription",
    "__version__",
    "build_logger",
]
