"""Core API freeze: the package root is the v1 contract downstream relies on."""

from __future__ import annotations

import korodexios_core

FROZEN_SURFACE = (
    "ModuleProtocol",
    "Module",
    "ModuleSpec",
    "ModuleState",
    "check_contract",
    "as_module",
    "CoreContext",
    "CliCommand",
    "CliArgument",
    "CliRequest",
    "CliSurface",
    "LayeredConfig",
    "ScopedConfig",
    "CORE_DEFAULTS",
    "ConfigSource",
    "EventBus",
    "Event",
    "Subscription",
    "Chassis",
    "ChassisOptions",
    "ChassisState",
    "ModuleRegistry",
    "IpcServer",
    "IpcOptions",
    "IpcDiscovery",
    "IpcError",
    "AppPaths",
    "ExitCode",
    "KorodexiosError",
    "KorodexiosLogger",
    "build_logger",
    "CORE_API_VERSION",
    "__version__",
)


V2_SURFACE = (
    "ConfigKind",
    "ConfigField",
    "ConfigSchema",
    "ConfigIssue",
    "InstanceLock",
    "LockHolder",
    "InstanceLockError",
    "IpcRoute",
    "IpcRequest",
    "AsyncRuntime",
    "WorkerSupervisor",
    "WorkerRecord",
    "DiagnosticCheck",
    "DiagnosticSeverity",
    "REDACTION",
    "is_secret_key",
    "redact_value",
    "IpcResponse",
    "IpcStaticMount",
    "guess_content_type",
    "NamedLock",
    "NamedLockRegistry",
    "LockTimeoutError",
    "TaskScheduler",
    "ScheduledTask",
)


def test_every_exported_name_resolves() -> None:
    for name in korodexios_core.__all__:
        assert getattr(korodexios_core, name) is not None, name


def test_public_surface_is_unique_and_freeze_complete() -> None:
    # Ordering is ruff's job (RUF022); this test guards the contract itself.
    exported = list(korodexios_core.__all__)
    assert len(set(exported)) == len(exported), "duplicate export in __all__"
    missing = sorted(name for name in FROZEN_SURFACE if name not in set(exported))
    assert not missing, f"core API lost a frozen name: {missing}"


def test_core_api_version_is_frozen_at_two() -> None:
    assert korodexios_core.CORE_API_VERSION == "2"
    assert korodexios_core.__version__.startswith("0.2.0")


def test_core_api_v2_names_are_exported() -> None:
    # Core API v2 grew additively: these names must resolve from the package root.
    for name in V2_SURFACE:
        assert getattr(korodexios_core, name) is not None, name


def test_frozen_contract_is_importable_from_the_package_root() -> None:
    assert korodexios_core.Module.__name__ == "Module"
    assert korodexios_core.ModuleState.NEW.value == "new"
    assert korodexios_core.ExitCode.SUCCESS == 0
    assert korodexios_core.check_contract(object())  # a bare object fails, loudly
