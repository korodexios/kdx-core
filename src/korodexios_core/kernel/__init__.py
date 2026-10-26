"""The microkernel: contract, discovery, registry, context, signals, chassis."""

from __future__ import annotations

from .async_runtime import AsyncRuntime
from .chassis import Chassis, ChassisOptions, ChassisState
from .cli_contract import CliArgument, CliCommand, CliRequest, CliSurface, command_path
from .context import CoreContext
from .contract import (
    Module,
    ModuleProtocol,
    ModuleSpec,
    ModuleState,
    as_module,
    check_contract,
    contract_help,
)
from .contracts import (
    ContractDrift,
    ContractReport,
    ContractRow,
    contract_rows,
    verify_contracts,
)
from .diagnostics import (
    DiagnosticCheck,
    DiagnosticSeverity,
    aggregate_diagnostics,
    diagnostic_check,
)
from .discovery import (
    DiscoveryCandidate,
    DiscoveryFailure,
    DiscoveryOptional,
    DiscoveryReport,
    ModuleDiscovery,
)
from .instance_lock import InstanceLock, LockHolder, pid_alive
from .ipc import (
    IpcRequest,
    IpcResponse,
    IpcRoute,
    IpcStaticMount,
    guess_content_type,
)
from .locks import LockTimeoutError, NamedLock, NamedLockRegistry
from .project import ProjectIdentity, project_identity
from .registry import ModuleRegistry, RegisteredModule, call_with_timeout
from .scheduler import ScheduledTask, TaskScheduler
from .signals import SignalCoordinator, signal_name
from .supervision import WorkerRecord, WorkerSupervisor

__all__ = [
    "AsyncRuntime",
    "Chassis",
    "ChassisOptions",
    "ChassisState",
    "CliArgument",
    "CliCommand",
    "CliRequest",
    "CliSurface",
    "ContractDrift",
    "ContractReport",
    "ContractRow",
    "CoreContext",
    "DiagnosticCheck",
    "DiagnosticSeverity",
    "DiscoveryCandidate",
    "DiscoveryFailure",
    "DiscoveryOptional",
    "DiscoveryReport",
    "InstanceLock",
    "IpcRequest",
    "IpcResponse",
    "IpcRoute",
    "IpcStaticMount",
    "LockHolder",
    "LockTimeoutError",
    "Module",
    "ModuleDiscovery",
    "ModuleProtocol",
    "ModuleRegistry",
    "ModuleSpec",
    "ModuleState",
    "NamedLock",
    "NamedLockRegistry",
    "ProjectIdentity",
    "RegisteredModule",
    "ScheduledTask",
    "SignalCoordinator",
    "TaskScheduler",
    "WorkerRecord",
    "WorkerSupervisor",
    "aggregate_diagnostics",
    "as_module",
    "call_with_timeout",
    "check_contract",
    "command_path",
    "contract_help",
    "contract_rows",
    "diagnostic_check",
    "guess_content_type",
    "pid_alive",
    "project_identity",
    "signal_name",
    "verify_contracts",
]
