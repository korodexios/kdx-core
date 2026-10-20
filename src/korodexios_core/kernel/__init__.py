"""The microkernel: contract, discovery, registry, context, signals, chassis."""

from __future__ import annotations

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
from .discovery import DiscoveryCandidate, DiscoveryFailure, DiscoveryReport, ModuleDiscovery
from .registry import ModuleRegistry, RegisteredModule, call_with_timeout
from .signals import SignalCoordinator, signal_name

__all__ = [
    "Chassis",
    "ChassisOptions",
    "ChassisState",
    "CliArgument",
    "CliCommand",
    "CliRequest",
    "CliSurface",
    "CoreContext",
    "DiscoveryCandidate",
    "DiscoveryFailure",
    "DiscoveryReport",
    "Module",
    "ModuleDiscovery",
    "ModuleProtocol",
    "ModuleRegistry",
    "ModuleSpec",
    "ModuleState",
    "RegisteredModule",
    "SignalCoordinator",
    "as_module",
    "call_with_timeout",
    "check_contract",
    "command_path",
    "contract_help",
    "signal_name",
]
