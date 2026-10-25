"""Module registry: the single owner of module state, ordering, and CLI wiring."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from ..config.schema import ConfigField, ConfigIssue, ConfigSchema, schema_from_mapping
from ..constants import DEFAULT_STOP_TIMEOUT
from ..events.bus import EventBus
from ..exceptions import ModuleContractError
from .cli_contract import CliCommand, CliSurface
from .context import CoreContext
from .contract import ModuleSpec, ModuleState, check_contract
from .diagnostics import DiagnosticCheck
from .discovery import DiscoveryReport
from .ipc import IpcRoute, IpcStaticMount

_PHASE_EVENTS = {"init": "initialized", "start": "started", "stop": "stopped"}
_PHASE_SUCCESS_STATE = {
    "init": ModuleState.INITIALIZED,
    "start": ModuleState.RUNNING,
    "stop": ModuleState.STOPPED,
}


def call_with_timeout(
    func: Callable[[], None],
    timeout: float | None,
    *,
    description: str = "call",
) -> tuple[bool, BaseException | None]:
    """Run *func* on a worker thread; return (completed, error)."""
    if not timeout or timeout <= 0:
        try:
            func()
        except BaseException as exc:
            return False, exc
        return True, None

    box: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            func()
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=_runner, name=f"kdx-{description}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return False, TimeoutError(f"{description} did not finish within {timeout:.1f}s")
    return ("error" not in box), box.get("error")


def _call_init(record: RegisteredModule, context: CoreContext) -> None:
    """Invoke the module init hook with the chassis context."""
    record.module.init(context)


@dataclass(slots=True)
class RegisteredModule:
    """One module plus everything the chassis learned while driving it."""

    spec: ModuleSpec
    module: Any
    state: ModuleState = ModuleState.NEW
    initialized: bool = False
    error: str | None = None
    init_ms: float | None = None
    start_ms: float | None = None
    stop_ms: float | None = None
    commands: list[CliCommand] = field(default_factory=list)
    schema: ConfigSchema | None = None
    routes: tuple[IpcRoute, ...] = ()
    statics: tuple[IpcStaticMount, ...] = ()
    checks: tuple[DiagnosticCheck, ...] = ()
    declarations_after_init: bool = False
    """True once the declarative hooks were read with the module context in hand."""

    def declared_keys(self) -> tuple[str, ...]:
        """Return the configuration keys this module declared."""
        return self.schema.keys() if self.schema is not None else ()

    def describe(self) -> str:
        """Return the one-line status line for 'kdx modules list'."""
        marker = {
            ModuleState.RUNNING: "[running]",
            ModuleState.INITIALIZED: "[ready]",
            ModuleState.STOPPED: "[stopped]",
            ModuleState.FAILED: "[failed]",
            ModuleState.DISABLED: "[disabled]",
            ModuleState.NEW: "[new]",
        }[self.state]
        return f"{marker} {self.spec.describe()}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view used by 'kdx doctor'."""
        return {
            "name": self.spec.name,
            "version": self.spec.version,
            "description": self.spec.description,
            "origin": self.spec.origin,
            "priority": self.spec.priority,
            "depends_on": list(self.spec.depends_on),
            "state": self.state.value,
            "error": self.error,
            "timings_ms": {
                "init": round(self.init_ms, 1) if self.init_ms is not None else None,
                "start": round(self.start_ms, 1) if self.start_ms is not None else None,
                "stop": round(self.stop_ms, 1) if self.stop_ms is not None else None,
            },
            "commands": [command.name for command in self.commands],
            "config_keys": list(self.declared_keys()),
            "secret_keys": list(self.schema.secrets()) if self.schema is not None else [],
            "routes": [route.as_dict() for route in self.routes],
            "diagnostics": [check.as_dict() for check in self.checks],
            "declarations_after_init": self.declarations_after_init,
        }


class HeadlessSurface:
    """A command surface that accepts commands without rendering anything.

    The kernel collects the declarative command tree during `initialize()` so an
    embedded daemon answers `POST /api/v1/rpc` without importing the CLI. The commands
    still land in `RegisteredModule.commands`; nothing is printed or parsed here.
    """

    def add_command(self, command: CliCommand) -> None:
        """Accept *command*; a headless surface renders nothing."""


class _RecordingSurface:
    """Surface decorator capturing which commands a module contributed."""

    def __init__(self, inner: CliSurface, sink: list[CliCommand]) -> None:
        self._inner = inner
        self._sink = sink

    def add_command(self, command: CliCommand) -> None:
        """Forward to the real surface and remember the command."""
        self._sink.append(command)
        self._inner.add_command(command)


def _topological(records: list[RegisteredModule]) -> list[RegisteredModule]:
    """Order records so every module starts after the modules it depends on.

    Ties keep the (priority, name) order, so behaviour stays predictable across
    boots. A dependency cycle cannot be ordered, so those records keep their
    priority position instead of failing the whole chassis.
    """
    by_name = {record.spec.name: record for record in records}
    order: dict[str, int] = {}
    position = 0

    def visit(record: RegisteredModule, trail: tuple[str, ...]) -> None:
        nonlocal position
        name = record.spec.name
        if name in order or name in trail:
            return
        for dependency in record.spec.depends_on:
            target = by_name.get(dependency)
            if target is not None:
                visit(target, (*trail, name))
        order[name] = position
        position += 1

    for record in records:
        visit(record, ())
    return sorted(
        records,
        key=lambda record: (
            order.get(record.spec.name, 0),
            record.spec.priority,
            record.spec.name,
        ),
    )


def _dependency_cycles(records: list[RegisteredModule]) -> list[str]:
    """Return the dependency edges that close a cycle, if any."""
    by_name = {record.spec.name: record for record in records}
    finished: set[str] = set()
    visiting: set[str] = set()
    edges: list[str] = []

    def walk(record: RegisteredModule) -> None:
        visiting.add(record.spec.name)
        for dependency in record.spec.depends_on:
            target = by_name.get(dependency)
            if target is None:
                continue
            if target.spec.name in visiting:
                edges.append(f"{record.spec.name} -> {target.spec.name}")
            elif target.spec.name not in finished:
                walk(target)
        visiting.discard(record.spec.name)
        finished.add(record.spec.name)

    for record in records:
        if record.spec.name not in finished:
            walk(record)
    return sorted(set(edges))


def _call_hook(module: Any, name: str, logger: Any) -> Any:
    """Call an optional module hook, isolating a broken one from the boot.

    The chassis collects the declarative hooks when a module registers, which is before
    `init(context)` has run: a module that reads its configuration, paths, or logger in
    `ipc_routes()` or `diagnostics()` therefore raises `ModuleContractError` on the first
    pass. That is not a boot failure - the hooks are collected again once `init()` ran - so
    the expected case is logged at debug level and the module keeps its baseline report.
    """
    hook = getattr(module, name, None)
    if not callable(hook):
        return None
    try:
        return hook()
    except ModuleContractError as exc:
        logger.debug(
            f"module hook {name} ran before init(context): {exc}",
            module=getattr(module, "name", "?"),
        )
        return None
    except Exception as exc:
        logger.error(
            f"module hook {name} failed: {type(exc).__name__}: {exc}",
            module=getattr(module, "name", "?"),
        )
        return None


def collect_schema(module: Any, name: str, logger: Any) -> ConfigSchema | None:
    """Normalize whatever config_schema() returns into a ConfigSchema."""
    declared = _call_hook(module, "config_schema", logger)
    if declared is None:
        return None
    if isinstance(declared, ConfigSchema):
        if not declared.module:
            return declared
        return ConfigSchema(module=name, fields=declared.fields)
    if isinstance(declared, ConfigField):
        return ConfigSchema(module=name, fields=(declared,))
    if isinstance(declared, Mapping | list | tuple):
        try:
            schema = schema_from_mapping(declared, module=name)
        except ValueError as exc:
            logger.error(f"module {name} declares an invalid configuration schema: {exc}")
            return None
        return schema if not schema.is_empty else None
    return None


def collect_ipc_routes(module: Any, name: str, logger: Any) -> tuple[IpcRoute, ...]:
    """Return the control-plane routes a module declares, skipping bad ones."""
    declared = _call_hook(module, "ipc_routes", logger)
    if declared is None:
        return ()
    if isinstance(declared, IpcRoute):
        declared = (declared,)
    routes: list[IpcRoute] = []
    for candidate in declared:
        if isinstance(candidate, IpcRoute):
            routes.append(
                candidate if candidate.module else _route_for_module(candidate, name)
            )
        else:
            logger.error(
                f"module {name} declared {candidate!r}, which is not an IpcRoute",
                module=name,
            )
    return tuple(routes)


def _route_for_module(route: IpcRoute, name: str) -> IpcRoute:
    """Return *route* tagged as belonging to module *name*."""
    from dataclasses import replace

    return replace(route, module=name)


def collect_static_dirs(module: Any, name: str, logger: Any) -> tuple[IpcStaticMount, ...]:
    """Return the static directories a module publishes, skipping bad declarations.

    A module declares a mount the same way it declares a route: once, in the
    optional hook. Directories that no longer exist are reported and dropped, so
    an uninstalled UI costs a warning at boot instead of a broken control plane.
    """
    declared = _call_hook(module, "static_dirs", logger)
    if declared is None:
        return ()
    if isinstance(declared, IpcStaticMount):
        declared = (declared,)
    mounts: list[IpcStaticMount] = []
    for candidate in declared:
        if isinstance(candidate, IpcStaticMount):
            mounts.append(
                candidate if candidate.module else _static_for_module(candidate, name)
            )
        else:
            logger.error(
                f"module {name} declared {candidate!r}, which is not a static mount",
                module=name,
            )
    return tuple(mounts)


def _static_for_module(mount: IpcStaticMount, name: str) -> IpcStaticMount:
    """Return *mount* attributed to module *name*."""
    from dataclasses import replace

    return replace(mount, module=name)


def collect_diagnostics(module: Any, name: str, logger: Any) -> tuple[DiagnosticCheck, ...]:
    """Return the diagnostic checks a module contributes to 'kdx doctor'.

    A module queried before `init(context)` that raised `ModuleContractError` contributes no
    checks on that pass; the chassis asks again after `init()` ran.
    """
    declared = _call_hook(module, "diagnostics", logger)
    if declared is None:
        return ()
    if isinstance(declared, DiagnosticCheck):
        declared = (declared,)
    checks: list[DiagnosticCheck] = []
    for candidate in declared:
        if isinstance(candidate, DiagnosticCheck):
            checks.append(candidate if candidate.module else _check_for_module(candidate, name))
        else:
            logger.error(
                f"module {name} reported {candidate!r}, which is not a diagnostic check",
                module=name,
            )
    return tuple(checks)


def _check_for_module(check: DiagnosticCheck, name: str) -> DiagnosticCheck:
    """Return *check* attributed to module *name*."""
    from dataclasses import replace

    return replace(check, module=name)


def collect_workers(module: Any) -> tuple[tuple[str, Any], ...]:
    """Return (name, thread) pairs a module hands to supervision."""
    declared = getattr(module, "workers", None)
    if not callable(declared):
        return ()
    try:
        result = declared()
    except Exception:
        return ()
    if result is None:
        return ()
    pairs: list[tuple[str, Any]] = []
    if isinstance(result, Mapping):
        for name, thread in result.items():
            pairs.append((str(name), thread))
    elif isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, threading.Thread):
                pairs.append((item.name, item))
            elif isinstance(item, tuple) and len(item) == 2:
                pairs.append((str(item[0]), item[1]))
    return tuple(
        (name, thread)
        for name, thread in pairs
        if isinstance(thread, threading.Thread)
    )


class ModuleRegistry:
    """Holds the loaded modules and drives their lifecycle with error isolation."""

    def __init__(self, logger: Any, *, bus: EventBus | None = None) -> None:
        self._logger = logger
        self._bus = bus
        self._records: dict[str, RegisteredModule] = {}
        self._lock = threading.RLock()

    def register(self, module: Any, spec: ModuleSpec) -> RegisteredModule:
        """Validate and store *module* under *spec*."""
        spec.validate()
        violations = check_contract(module)
        if violations:
            raise ModuleContractError(
                f"module {spec.name!r} breaks the contract: {'; '.join(violations)}",
                hint="Expose name, description, version, init, register_cli, start, stop.",
            )
        with self._lock:
            if spec.name in self._records:
                raise ModuleContractError(
                    f"module name {spec.name!r} is already registered",
                    hint="Rename the module directory or disable the duplicate.",
                )
            record = RegisteredModule(spec=spec, module=module)
            record.schema = collect_schema(module, spec.name, self._logger)
            record.routes = collect_ipc_routes(module, spec.name, self._logger)
            record.statics = collect_static_dirs(module, spec.name, self._logger)
            record.checks = collect_diagnostics(module, spec.name, self._logger)
            if not spec.enabled:
                record.state = ModuleState.DISABLED
            self._records[spec.name] = record
        self._publish(f"module.{spec.name}.registered", {"version": spec.version})
        self._logger.debug(
            "module registered",
            module=spec.name,
            version=spec.version,
            origin=spec.origin,
            priority=spec.priority,
        )
        return record

    def register_discovered(self, report: DiscoveryReport) -> list[RegisteredModule]:
        """Register every candidate from a discovery sweep, rejecting bad ones."""
        added: list[RegisteredModule] = []
        for candidate in report.candidates:
            try:
                added.append(self.register(candidate.instance, candidate.spec))
            except ModuleContractError as exc:
                self._logger.error(f"rejected module {candidate.spec.name}: {exc.message}")
        return added

    def get(self, name: str) -> RegisteredModule | None:
        """Return the record for *name*, or None."""
        with self._lock:
            return self._records.get(str(name))

    def require(self, name: str) -> RegisteredModule:
        """Return the record for *name* or raise ModuleContractError."""
        record = self.get(name)
        if record is None:
            raise ModuleContractError(
                f"module {str(name)!r} is not registered",
                hint=f"Loaded modules: {', '.join(self.names()) or '(none)'}.",
            )
        return record

    def names(self) -> list[str]:
        """Return registered module names in load order."""
        return [record.spec.name for record in self.ordered()]

    def ordered(self) -> list[RegisteredModule]:
        """Return records in lifecycle order: dependencies first, then priority."""
        with self._lock:
            return _topological(self._by_priority())

    def _by_priority(self) -> list[RegisteredModule]:
        """Return records sorted by (priority, name); callers hold the lock."""
        return sorted(self._records.values(), key=lambda item: (item.spec.priority, item.spec.name))

    def dependency_report(self) -> dict[str, list[str]]:
        """Return unknown dependencies and cycles so diagnostics can show them."""
        with self._lock:
            records = self._by_priority()
            names = {record.spec.name for record in records}
            missing = [
                f"{record.spec.name} -> {dependency}"
                for record in records
                for dependency in record.spec.depends_on
                if dependency not in names
            ]
            return {"missing": missing, "cycles": _dependency_cycles(records)}

    def active(self) -> list[RegisteredModule]:
        """Return records that are not disabled."""
        return [record for record in self.ordered() if record.state is not ModuleState.DISABLED]

    def commands(self) -> list[CliCommand]:
        """Return every command contributed by registered modules."""
        collected: list[CliCommand] = []
        for record in self.ordered():
            collected.extend(record.commands)
        return collected

    def collect_declarations(self, name: str) -> RegisteredModule:
        """Collect one module's declarative hooks again, now that `init(context)` ran.

        Registration reads the hooks before the module has a context, so a hook that
        inspects configuration, paths, or its logger can only answer truthfully here. The
        hooks are declarative, so collecting them twice is idempotent - and this second
        pass is what `kdx doctor`, `kdx config check`, and the control plane end up using.
        """
        record = self.require(name)
        label = record.spec.name
        record.schema = collect_schema(record.module, label, self._logger)
        record.routes = collect_ipc_routes(record.module, label, self._logger)
        record.statics = collect_static_dirs(record.module, label, self._logger)
        record.checks = collect_diagnostics(record.module, label, self._logger)
        record.declarations_after_init = True
        return record

    def initialize_all(self, context: CoreContext) -> list[RegisteredModule]:
        """Call init(context) on every new module, in load order, then re-read its hooks."""
        results: list[RegisteredModule] = []
        for record in self.ordered():
            if record.state is not ModuleState.NEW:
                continue
            results.append(self._run(record, "init", partial(_call_init, record, context)))
            if record.initialized:
                self.collect_declarations(record.spec.name)
        return results

    def collect_headless(self) -> list[CliCommand]:
        """Collect the command tree for the kernel, with no user interface attached."""
        return self.collect_cli(HeadlessSurface())

    def start_all(self) -> list[RegisteredModule]:
        """Call start() on every initialized module, in load order."""
        results: list[RegisteredModule] = []
        for record in self.ordered():
            if record.state is not ModuleState.INITIALIZED:
                continue
            results.append(self._run(record, "start", record.module.start))
        return results

    def stop_all(self, timeout: float | None = DEFAULT_STOP_TIMEOUT) -> list[RegisteredModule]:
        """Call stop() on every initialized module, newest first, with a timeout."""
        results: list[RegisteredModule] = []
        for record in reversed(self.ordered()):
            if not record.initialized or record.state is ModuleState.STOPPED:
                continue
            results.append(self._run(record, "stop", record.module.stop, timeout=timeout))
        return results

    def collect_cli(self, surface: CliSurface) -> list[CliCommand]:
        """Let every module inject its commands into *surface*; return them all.

        `register_cli` runs exactly once per module. A module whose commands are already
        recorded - the kernel collects them headlessly at `initialize()` - has those same
        command objects replayed into *surface* instead of being asked twice, so argparse,
        a wizard, and a docs generator all see one authoritative tree.
        """
        collected: list[CliCommand] = []
        for record in self.ordered():
            if record.state is ModuleState.DISABLED:
                continue
            if record.commands:
                for command in record.commands:
                    surface.add_command(command)
                collected.extend(record.commands)
                continue
            sink: list[CliCommand] = []
            try:
                record.module.register_cli(_RecordingSurface(surface, sink))
            except Exception as exc:
                self._logger.error(
                    f"module {record.spec.name} contributed no commands: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            record.commands = list(sink)
            collected.extend(sink)
        return collected

    def schemas(self) -> dict[str, ConfigSchema]:
        """Return every module configuration schema, keyed by module name."""
        with self._lock:
            return {
                record.spec.name: record.schema
                for record in self._records.values()
                if record.schema is not None and not record.schema.is_empty
            }

    def ipc_routes(self) -> list[IpcRoute]:
        """Return the routes every registered module declares."""
        routes: list[IpcRoute] = []
        for record in self.ordered():
            routes.extend(record.routes)
        return routes

    def static_dirs(self) -> list[IpcStaticMount]:
        """Return the static directories every registered module publishes."""
        mounts: list[IpcStaticMount] = []
        for record in self.ordered():
            mounts.extend(record.statics)
        return mounts

    def diagnostics(self) -> list[DiagnosticCheck]:
        """Return the diagnostic checks every registered module contributes."""
        checks: list[DiagnosticCheck] = []
        for record in self.ordered():
            checks.extend(record.checks)
        return checks

    def declared_workers(self) -> dict[str, tuple[tuple[str, Any], ...]]:
        """Return the workers each module reports for supervision."""
        with self._lock:
            return {
                record.spec.name: collect_workers(record.module)
                for record in self._records.values()
                if collect_workers(record.module)
            }

    def validate_schemas(self, config: Any) -> list[ConfigIssue]:
        """Validate every module schema against *config*; return the findings."""
        issues: list[ConfigIssue] = []
        for name, schema in sorted(self.schemas().items()):
            prefix = f"modules.{name}"
            origins: dict[str, str] = {}
            for declared_field in schema.fields:
                full = f"{prefix}.{declared_field.key}"
                if config is not None and hasattr(config, "origin_of"):
                    origins[declared_field.key] = str(config.origin_of(full))
            issues.extend(schema.validate(config, prefix=prefix, origins=origins))
        return issues

    def health_for(self, name: str) -> dict[str, Any] | None:
        """Return one module's report, or None when the chassis never loaded it."""
        with self._lock:
            record = self._records.get(str(name))
        return record.as_dict() if record is not None else None

    def notify_config_reload(self, config: Any) -> int:
        """Hand each module its fresh scoped view; return how many hooks ran."""
        notified = 0
        for record in self.ordered():
            hook = getattr(record.module, "on_config_reload", None)
            if not callable(hook):
                continue
            scoped = config.child(f"modules.{record.spec.name}")
            try:
                hook(scoped)
            except Exception as exc:
                self._logger.error(
                    f"module {record.spec.name} failed to react to the reload: "
                    f"{type(exc).__name__}: {exc}",
                    module=record.spec.name,
                    phase="reload",
                )
                continue
            notified += 1
        return notified

    def mark_failed(self, name: str, reason: str) -> RegisteredModule | None:
        """Move one module to FAILED because the chassis noticed something wrong."""
        with self._lock:
            record = self._records.get(str(name))
            if record is None:
                return None
            already_failed = record.state is ModuleState.FAILED
            record.state = ModuleState.FAILED
            record.error = str(reason)
        self._logger.error(
            f"module {name} marked failed: {reason}", module=str(name), phase="supervision"
        )
        if not already_failed:
            self._publish(
                f"module.{name}.failed", {"phase": "supervision", "error": str(reason)}
            )
        return record

    def counts(self) -> dict[str, int]:
        """Return how many modules sit in each state."""
        counts: dict[str, int] = {state.value: 0 for state in ModuleState}
        for record in self.ordered():
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return counts

    def health(self) -> dict[str, Any]:
        """Return the module health block shown by 'kdx doctor'."""
        records = self.ordered()
        return {
            "total": len(records),
            "by_state": self.counts(),
            "failed": [
                record.spec.name for record in records if record.state is ModuleState.FAILED
            ],
            "modules": [record.as_dict() for record in records],
        }

    def _run(
        self,
        record: RegisteredModule,
        phase: str,
        call: Callable[[], None],
        *,
        timeout: float | None = None,
    ) -> RegisteredModule:
        started = time.perf_counter()
        completed, error = call_with_timeout(
            call, timeout, description=f"{record.spec.name}.{phase}"
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if completed and error is None:
            self._store_timing(record, phase, elapsed_ms)
            record.state = _PHASE_SUCCESS_STATE[phase]
            if phase == "init":
                record.initialized = True
            record.error = None
            self._logger.debug(
                f"module {record.spec.name} {phase} ok",
                module=record.spec.name,
                ms=round(elapsed_ms, 1),
            )
            self._publish(
                f"module.{record.spec.name}.{_PHASE_EVENTS[phase]}",
                {"version": record.spec.version, "ms": round(elapsed_ms, 1)},
            )
        else:
            record.state = ModuleState.FAILED
            record.error = str(error) if error is not None else f"{phase} failed"
            self._logger.error(
                f"module {record.spec.name} failed during {phase}: {record.error}",
                module=record.spec.name,
                phase=phase,
            )
            self._publish(
                f"module.{record.spec.name}.failed",
                {"phase": phase, "error": record.error},
            )
        return record

    @staticmethod
    def _store_timing(record: RegisteredModule, phase: str, elapsed_ms: float) -> None:
        if phase == "init":
            record.init_ms = elapsed_ms
        elif phase == "start":
            record.start_ms = elapsed_ms
        elif phase == "stop":
            record.stop_ms = elapsed_ms

    def _publish(self, name: str, payload: Any = None) -> None:
        if self._bus is not None:
            self._bus.publish(name, payload, source="registry")

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.ordered())

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return str(name) in self._records

    def __repr__(self) -> str:
        return f"ModuleRegistry(modules={self.names()})"
