"""korodexios-core: a headless microkernel chassis for Korodexios projects.

The names in __all__ are the frozen core API (core api v1): module contract,
declarative CLI contract, layered configuration, event bus, chassis lifecycle, and
the loopback control plane. Additive changes are allowed inside v1; anything that
breaks an import or a signature here moves the major version.
"""

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
from .kernel.chassis import Chassis, ChassisOptions, ChassisState, stdin_is_tty
from .kernel.cli_contract import (
    CliArgument,
    CliCommand,
    CliRequest,
    CliSurface,
    command_path,
)
from .kernel.context import CoreContext
from .kernel.contract import (
    Module,
    ModuleProtocol,
    ModuleSpec,
    ModuleState,
    as_module,
    check_contract,
)
from .kernel.ipc import IpcDiscovery, IpcError, IpcOptions, IpcServer
from .kernel.registry import ModuleRegistry
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
    "Chassis",
    "ChassisError",
    "ChassisOptions",
    "ChassisState",
    "CliArgument",
    "CliCommand",
    "CliError",
    "CliRequest",
    "CliSurface",
    "ConfigFileError",
    "ConfigSource",
    "ConfigValueError",
    "ConfigurationError",
    "CoreContext",
    "Event",
    "EventBus",
    "ExitCode",
    "IpcDiscovery",
    "IpcError",
    "IpcOptions",
    "IpcServer",
    "KorodexError",
    "KorodexLogger",
    "LayeredConfig",
    "LoggerFactory",
    "Module",
    "ModuleContractError",
    "ModuleDiscoveryError",
    "ModuleLifecycleError",
    "ModuleProtocol",
    "ModuleRegistry",
    "ModuleSpec",
    "ModuleState",
    "PathDiscoveryError",
    "ScopedConfig",
    "Subscription",
    "__version__",
    "as_module",
    "build_logger",
    "check_contract",
    "command_path",
    "stdin_is_tty",
]
