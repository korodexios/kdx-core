"""Module registry: the single owner of module state, ordering, and CLI wiring."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from ..constants import DEFAULT_STOP_TIMEOUT
from ..events.bus import EventBus
from ..exceptions import ModuleContractError
from .cli_contract import CliCommand, CliSurface
from .context import CoreContext
from .contract import ModuleSpec, ModuleState, check_contract
from .discovery import DiscoveryReport

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

    worker = threading.Thread(target=_runner, name=f"korodex-{description}", daemon=True)
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

    def describe(self) -> str:
        """Return the one-line status line for 'korodex modules list'."""
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
        """Return a JSON-friendly view used by 'korodex doctor'."""
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
        }


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

    def initialize_all(self, context: CoreContext) -> list[RegisteredModule]:
        """Call init(context) on every new module, in load order."""
        results: list[RegisteredModule] = []
        for record in self.ordered():
            if record.state is not ModuleState.NEW:
                continue
            results.append(self._run(record, "init", partial(_call_init, record, context)))
        return results

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
        """Let every module inject its commands into *surface*; return them all."""
        collected: list[CliCommand] = []
        for record in self.ordered():
            if record.state is ModuleState.DISABLED:
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

    def counts(self) -> dict[str, int]:
        """Return how many modules sit in each state."""
        counts: dict[str, int] = {state.value: 0 for state in ModuleState}
        for record in self.ordered():
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return counts

    def health(self) -> dict[str, Any]:
        """Return the module health block shown by 'korodex doctor'."""
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
