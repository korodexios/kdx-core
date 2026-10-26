"""korodexios-core: a headless microkernel chassis for Korodexios projects.

The names in __all__ are the core API: module contract, declarative CLI contract,
layered configuration and schemas, event bus, chassis lifecycle, worker supervision,
the loopback control plane and its declarable routes. Additive changes are allowed
inside a major version; anything that breaks an import or a signature here moves it.
"""

from __future__ import annotations

from .config import (
    CORE_DEFAULTS,
    ConfigField,
    ConfigIssue,
    ConfigKind,
    ConfigSchema,
    ConfigSource,
    LayeredConfig,
    ScopedConfig,
    is_secret_key,
    redact_value,
)
from .constants import REDACTION, TAG_ASK, TAG_ERROR, TAG_NOTE, TAG_OK, ExitCode
from .events import Event, EventBus, Subscription
from .exceptions import (
    ChassisError,
    CliError,
    ConfigFileError,
    ConfigurationError,
    ConfigValueError,
    InstanceLockError,
    KorodexiosError,
    LockTimeoutError,
    ModuleContractError,
    ModuleDiscoveryError,
    ModuleLifecycleError,
    PathDiscoveryError,
)
from .kernel.async_runtime import AsyncRuntime
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
from .kernel.contracts import (
    ContractDrift,
    ContractReport,
    ContractRow,
    contract_rows,
    verify_contracts,
)
from .kernel.diagnostics import DiagnosticCheck, DiagnosticSeverity
from .kernel.instance_lock import InstanceLock, LockHolder
from .kernel.ipc import (
    IpcDiscovery,
    IpcError,
    IpcOptions,
    IpcRequest,
    IpcResponse,
    IpcRoute,
    IpcServer,
    IpcStaticMount,
    guess_content_type,
)
from .kernel.locks import NamedLock, NamedLockRegistry
from .kernel.project import ProjectIdentity, project_identity
from .kernel.registry import ModuleRegistry
from .kernel.scheduler import ScheduledTask, TaskScheduler
from .kernel.supervision import WorkerRecord, WorkerSupervisor
from .logging import KorodexiosLogger, LoggerFactory, build_logger
from .paths import AppPaths
from .version import CORE_API_VERSION, __version__

__all__ = [
    "CORE_API_VERSION",
    "CORE_DEFAULTS",
    "REDACTION",
    "TAG_ASK",
    "TAG_ERROR",
    "TAG_NOTE",
    "TAG_OK",
    "AppPaths",
    "AsyncRuntime",
    "Chassis",
    "ChassisError",
    "ChassisOptions",
    "ChassisState",
    "CliArgument",
    "CliCommand",
    "CliError",
    "CliRequest",
    "CliSurface",
    "ConfigField",
    "ConfigFileError",
    "ConfigIssue",
    "ConfigKind",
    "ConfigSchema",
    "ConfigSource",
    "ConfigValueError",
    "ConfigurationError",
    "ContractDrift",
    "ContractReport",
    "ContractRow",
    "CoreContext",
    "DiagnosticCheck",
    "DiagnosticSeverity",
    "Event",
    "EventBus",
    "ExitCode",
    "InstanceLock",
    "InstanceLockError",
    "IpcDiscovery",
    "IpcError",
    "IpcOptions",
    "IpcRequest",
    "IpcResponse",
    "IpcRoute",
    "IpcServer",
    "IpcStaticMount",
    "KorodexiosError",
    "KorodexiosLogger",
    "LayeredConfig",
    "LockHolder",
    "LockTimeoutError",
    "LoggerFactory",
    "Module",
    "ModuleContractError",
    "ModuleDiscoveryError",
    "ModuleLifecycleError",
    "ModuleProtocol",
    "ModuleRegistry",
    "ModuleSpec",
    "ModuleState",
    "NamedLock",
    "NamedLockRegistry",
    "PathDiscoveryError",
    "ProjectIdentity",
    "ScheduledTask",
    "ScopedConfig",
    "Subscription",
    "TaskScheduler",
    "WorkerRecord",
    "WorkerSupervisor",
    "__version__",
    "as_module",
    "build_logger",
    "check_contract",
    "command_path",
    "contract_rows",
    "guess_content_type",
    "is_secret_key",
    "project_identity",
    "redact_value",
    "stdin_is_tty",
    "verify_contracts",
]
