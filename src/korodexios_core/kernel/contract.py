"""The module contract: the exact surface every chassis plugin must expose."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..constants import DEFAULT_STOP_TIMEOUT
from ..exceptions import ModuleContractError
from .cli_contract import CliSurface

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config.layered import ScopedConfig
    from ..events.bus import EventBus
    from ..logging.engine import KorodexiosLogger
    from ..paths import AppPaths
    from .context import CoreContext

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+[A-Za-z0-9._+-]*$")
REQUIRED_MEMBERS: tuple[str, ...] = (
    "name",
    "description",
    "version",
    "init",
    "register_cli",
    "start",
    "stop",
)


class ModuleState(StrEnum):
    """Lifecycle states tracked by the registry for each module."""

    NEW = "new"
    INITIALIZED = "initialized"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """Discovery metadata describing where a module came from and how to load it."""

    name: str
    description: str = ""
    version: str = "0.0.0"
    entrypoint: str = "MODULE"
    enabled: bool = True
    priority: int = 100
    depends_on: tuple[str, ...] = ()
    origin: str = "builtin"
    source: Path | None = None
    requires_core: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Raise ModuleContractError when the metadata is unusable."""
        if not NAME_PATTERN.match(self.name):
            raise ModuleContractError(
                f"module name {self.name!r} must be lower_case with digits or underscores",
                hint="Name the module after its directory, for example 'backup_engine'.",
            )
        if not VERSION_PATTERN.match(self.version):
            raise ModuleContractError(
                f"module {self.name!r} version {self.version!r} must look like '0.1.0'",
                hint="Use MAJOR.MINOR.PATCH so the chassis can gate features.",
            )
        for dependency in self.depends_on:
            if not NAME_PATTERN.match(str(dependency)):
                raise ModuleContractError(
                    f"module {self.name!r} depends on {dependency!r}, not a valid module name",
                    hint="List dependency names exactly as their module directories.",
                )
        if self.priority < 0:
            raise ModuleContractError(
                f"module {self.name!r} priority must be zero or greater, got {self.priority}"
            )

    def describe(self) -> str:
        """Return the one-line description used by 'kdx modules list'."""
        origin = f" ({self.origin})" if self.origin else ""
        return f"{self.name} {self.version}{origin}: {self.description or 'no description'}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the spec."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "entrypoint": self.entrypoint,
            "enabled": self.enabled,
            "priority": self.priority,
            "depends_on": list(self.depends_on),
            "origin": self.origin,
            "source": str(self.source) if self.source else None,
            "requires_core": self.requires_core,
        }


@runtime_checkable
class ModuleProtocol(Protocol):
    """Structural contract. Any object with these members can drive the chassis."""

    name: str
    description: str
    version: str

    def init(self, context: CoreContext) -> None:
        """Receive the core context: config, logger, event bus, paths."""
        ...

    def register_cli(self, surface: CliSurface) -> None:
        """Inject this module's commands into the root CLI surface."""
        ...

    def start(self) -> None:
        """Start background work (threads, timers, sockets)."""
        ...

    def stop(self) -> None:
        """Dispose of sockets, files, threads, and other resources."""
        ...


class Module:
    """Convenience base class implementing the contract with real defaults.

    The chassis drives a module in one order: discovered, registered, `init(context)`,
    `register_cli(surface)`, `start()`, `stop()`. The optional declarative hooks -
    `config_schema()`, `ipc_routes()`, `static_dirs()`, `diagnostics()`, `contracts()`
    are read at registration, before `init(context)` has run, and collected again once
    it has. A module that needs its configuration, paths, or logger to answer one of
    those hooks returns an empty or baseline report on the first pass and never raises:
    an inspection command such as `kdx doctor` may ask at any point in that sequence.
    """

    name: str = ""
    description: str = ""
    version: str = "0.0.0"

    def __init__(self) -> None:
        self._state = ModuleState.NEW
        self._context: CoreContext | None = None
        self._started_at: float | None = None
        self._stop_timeout: float = DEFAULT_STOP_TIMEOUT
        self._validate_identity()

    def _validate_identity(self) -> None:
        missing = [
            attribute
            for attribute in ("name", "description", "version")
            if not str(getattr(self, attribute, "")).strip()
        ]
        if missing:
            raise ModuleContractError(
                f"{type(self).__name__} must define {', '.join(missing)}",
                hint="Set class attributes name, description, and version.",
            )
        if not NAME_PATTERN.match(self.name):
            raise ModuleContractError(
                f"{type(self).__name__}.name {self.name!r} must be lower_case with digits or"
                + " underscores",
                hint="Rename the module directory to a lower_case identifier.",
            )
        if not VERSION_PATTERN.match(self.version):
            raise ModuleContractError(
                f"{type(self).__name__}.version {self.version!r} must look like '0.1.0'"
            )

    @property
    def state(self) -> ModuleState:
        """Return the module's current lifecycle state."""
        return self._state

    @property
    def context(self) -> CoreContext | None:
        """Return the context supplied during init, when initialized."""
        return self._context

    @property
    def started_at(self) -> float | None:
        """Return when start() ran, or None."""
        return self._started_at

    @property
    def stop_timeout(self) -> float:
        """Return the seconds the chassis waits for stop() to finish."""
        return self._stop_timeout

    def require_context(self) -> CoreContext:
        """Return the context or raise ModuleContractError when init() is missing."""
        if self._context is None:
            raise ModuleContractError(
                f"module {self.name!r} was used before init(context) ran",
                hint="The chassis always calls init() before register_cli(), start(), stop().",
            )
        return self._context

    @property
    def config(self) -> ScopedConfig:
        """Return this module's scoped configuration view."""
        return self.require_context().module_config(self.name)

    @property
    def logger(self) -> KorodexiosLogger:
        """Return a logger namespaced to this module."""
        return self.require_context().logger_for(self.name)

    @property
    def bus(self) -> EventBus:
        """Return the shared event bus."""
        return self.require_context().bus

    @property
    def paths(self) -> AppPaths:
        """Return the chassis filesystem layout."""
        return self.require_context().paths

    def init(self, context: CoreContext) -> None:
        """Store the context and mark the module initialized."""
        self._context = context
        self._state = ModuleState.INITIALIZED
        context.logger_for(self.name).debug("module initialized", version=self.version)

    def register_cli(self, surface: CliSurface) -> None:
        """Optional hook; the default contributes no commands."""
        return

    def start(self) -> None:
        """Mark the module running; override to launch background work."""
        self._state = ModuleState.RUNNING
        self._started_at = time.time()

    def stop(self) -> None:
        """Mark the module stopped; override to release resources."""
        self._state = ModuleState.STOPPED
        self._started_at = None

    def config_schema(self) -> Any:
        """Declare the configuration this module reads.

        Return ConfigField instances, a ConfigSchema, or a manifest-shaped mapping
        or list; the chassis normalizes all of them. The default declares nothing,
        which is a valid answer for a module with no configuration.
        """
        return ()

    def ipc_routes(self) -> tuple[Any, ...]:
        """Declare control-plane routes this module serves on the chassis listener."""
        return ()

    def static_dirs(self) -> tuple[Any, ...]:
        """Declare directories this module serves as static content on the control plane.

        Return IpcStaticMount objects: a route prefix and a directory, and the
        chassis serves those files on its own port with the media type read from
        each file. A module that ships a dashboard declares the mount here instead
        of opening a web server of its own.
        """
        return ()

    def diagnostics(self) -> tuple[Any, ...]:
        """Contribute named checks, with severity and hint, to 'kdx doctor'.

        The chassis may ask before `init(context)` ran; return `()`, or checks that need nothing
        but class state, and the real findings once the context exists.
        """
        return ()

    def contracts(self) -> tuple[Any, ...]:
        """Declare the verified file set this module owns.

        Return ContractRow objects, or mappings carrying the keys path, sha256,
        classification, and merge_policy; 'kdx contracts check' hashes each file and
        reports drift without rewriting a single byte. The classification and merge
        policy words belong to the project - the chassis carries them and never reads
        them - and a module that owns no files returns ().
        """
        return ()

    @property
    def scheduler(self) -> Any:
        """Return the chassis periodic task scheduler; declare tasks in start().

        A scheduled task runs on the chassis clock with the chassis' own shutdown
        budget, so recurring work needs no thread, no stop flag, and no join.
        """
        return self.require_context().scheduler

    def workers(self) -> Any:
        """Return the background threads start() launched, for supervision.

        Accepts a mapping of name to thread or a sequence of (name, thread)
        pairs; the chassis watches each one and marks this module FAILED when an
        expected worker dies on its own.
        """
        return ()

    def on_config_reload(self, config: Any) -> None:
        """React to a chassis configuration reload.

        The chassis re-reads the layers, publishes core.config.reloaded, and calls
        this hook with the module's fresh scoped view. The default records the
        event so an operator can see the hook landed in the log.
        """
        if self._context is not None:
            self.logger.debug(
                "module notified of configuration reload", version=self.version
            )

    def watch_worker(
        self,
        thread: threading.Thread,
        *,
        name: str = "",
        expected: bool = True,
    ) -> Any:
        """Ask the chassis to supervise one of this module's threads."""
        supervisor = self.require_context().supervisor
        if supervisor is None:
            return None
        return supervisor.watch(self.name, thread, name=name, expected=expected)

    def health(self) -> dict[str, Any]:
        """Return the module's self-report shown by 'kdx modules show'.

        This hook may run before `init(context)` or after `stop()`, so a report built from context
        state has to tolerate both: the baseline below needs no context at all.
        """
        uptime = (time.time() - self._started_at) if self._started_at else 0.0
        return {
            "name": self.name,
            "version": self.version,
            "state": self._state.value,
            "uptime_seconds": round(uptime, 3),
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.name!r}, version={self.version!r}, "
            f"state={self._state.value!r})"
        )


def check_contract(candidate: Any) -> list[str]:
    """Return human-readable contract violations for *candidate* (empty when valid)."""
    violations: list[str] = []
    if candidate is None:
        return ["candidate is None"]
    for member in REQUIRED_MEMBERS:
        if not hasattr(candidate, member):
            violations.append(f"missing {member!r}")
    for attribute in ("name", "description", "version"):
        value = getattr(candidate, attribute, None)
        if value is not None and not isinstance(value, str):
            violations.append(f"{attribute} must be a string, got {type(value).__name__}")
    for hook in ("init", "register_cli", "start", "stop"):
        value = getattr(candidate, hook, None)
        if value is not None and not callable(value):
            violations.append(f"{hook} must be callable, got {type(value).__name__}")
    name_value = getattr(candidate, "name", "")
    if isinstance(name_value, str):
        name = str(name_value)
        if name and not NAME_PATTERN.match(name):
            violations.append(f"name {name!r} must be lower_case with digits or underscores")
    version_value = getattr(candidate, "version", "")
    if (
        isinstance(version_value, str)
        and version_value
        and not VERSION_PATTERN.match(version_value)
    ):
        violations.append(f"version {version_value!r} must look like '0.1.0'")
    return violations


def as_module(candidate: Any) -> Any:
    """Return *candidate* when it satisfies the contract, else raise."""
    violations = check_contract(candidate)
    if violations:
        label = type(candidate).__name__
        raise ModuleContractError(
            f"module candidate {label} breaks the contract: {'; '.join(violations)}",
            hint="Expose name, description, version, init, register_cli, start, stop.",
        )
    return candidate


def contract_help() -> str:
    """Return the contract cheat sheet printed by 'kdx modules show --help'."""
    return (
        "contract: name, description, version, init(context), "
        "register_cli(surface), start(), stop(); optional: dependencies, "
        "load_order, config_schema, ipc_routes, static_dirs, diagnostics, workers, contracts"
    )
