"""backup_engine: configurable source-to-destination backups with a dry run."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast

from ...cli.render import (
    dump_json,
    print_key_values,
    print_section,
    write_status,
)
from ...config.schema import ConfigField, ConfigKind, ConfigSchema
from ...constants import TAG_ERROR, TAG_NOTE, TAG_OK, ExitCode
from ...kernel.cli_contract import CliArgument, CliCommand, CliRequest, CliSurface
from ...kernel.context import CoreContext
from ...kernel.contract import Module
from ...kernel.diagnostics import DiagnosticCheck, DiagnosticSeverity
from ...kernel.ipc import IpcRequest, IpcRoute
from ...kernel.locks import LockTimeoutError, NamedLock
from ...kernel.scheduler import ScheduledTask
from ...units import format_bytes
from ...validate import parse_size
from .engine import (
    STAMP_FORMAT,
    BackupError,
    BackupPlan,
    BackupResult,
    BackupSettings,
    build_plan,
    default_stamp,
    describe_plan,
    describe_result,
    execute_plan,
)

DEFAULT_EXCLUDES = (".git", ".venv", "__pycache__", "var", "node_modules", "*.pyc", "*.log")
CONFIGURED_EVENT = "backup.configured"
COMPLETED_EVENT = "backup.completed"
PRUNED_EVENT = "backup.pruned"
SWEEP_TASK: Final[str] = "auto_sweep"
PRUNE_TASK: Final[str] = "auto_prune"
SECONDS_PER_DAY: Final[float] = 86_400.0


@dataclass(slots=True)
class PruneReport:
    """What one retention pass removed, how many copies it looked at, and what it freed.

    Reclaimed bytes are the sizes of the files the pass deleted, measured before the
    folder is gone: a retention sweep is space accounting an operator can check with
    'kdx doctor' against the destination it just thinned.
    """

    removed: int = 0
    scanned: int = 0
    reclaimed_bytes: int = 0


@dataclass(slots=True)
class RetentionReport:
    """The retention policy, the copies standing in the destination, and the sweep record.

    'expired_now' counts the standing copies the current window says the next sweep
    would remove; the sweep counters name this process, so a running daemon's own
    record answers through 'kdx status', 'kdx doctor', or 'kdx --remote backup
    retention', which runs the report inside the daemon.
    """

    dest: str = ""
    window_days: int = 0
    sweep_interval_seconds: float = 0.0
    sweep_scheduled: bool = False
    copies_standing: int = 0
    copies_bytes: int = 0
    expired_now: int = 0
    oldest_stamp: str = ""
    newest_stamp: str = ""
    sweeps: int = 0
    last_sweep_seconds_ago: float | None = None
    last_sweep_removed: int = 0
    removed_total: int = 0
    reclaimed_bytes: int = 0
    refusals: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return the machine shape: raw bytes plus a human rendering of each total."""
        return {
            "dest": self.dest,
            "window_days": self.window_days,
            "sweep_interval_seconds": self.sweep_interval_seconds,
            "sweep_scheduled": self.sweep_scheduled,
            "copies_standing": self.copies_standing,
            "copies_bytes": self.copies_bytes,
            "copies_human": format_bytes(self.copies_bytes),
            "expired_now": self.expired_now,
            "oldest_stamp": self.oldest_stamp,
            "newest_stamp": self.newest_stamp,
            "sweeps": self.sweeps,
            "last_sweep_seconds_ago": self.last_sweep_seconds_ago,
            "last_sweep_removed": self.last_sweep_removed,
            "removed_total": self.removed_total,
            "reclaimed_bytes": self.reclaimed_bytes,
            "reclaimed_human": format_bytes(self.reclaimed_bytes),
            "refusals": self.refusals,
        }


class BackupEngineModule(Module):
    """Backs up a source tree into a timestamped destination with validation."""

    name = "backup_engine"
    description = "Configurable source-to-destination backups with interactive setup and dry runs."
    # One copy per project: the run takes a named lock, so a second 'kdx backup run'
    # steps aside instead of copying the same tree twice into the same destination.
    LOCK_NAME: Final[str] = "backup_engine"
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._source = Path(".")
        self._dest = Path("backups")
        self._include: tuple[str, ...] = ("*",)
        self._exclude: tuple[str, ...] = DEFAULT_EXCLUDES
        self._max_bytes: int | None = None
        self._dry_run = False
        self._follow_symlinks = False
        self._runs = 0
        self._last: BackupResult | None = None
        self._refusals = 0
        self._last_refusal: str = ""
        self._schedule_interval = 0.0
        self._auto_prune_days = 0
        self._prune_interval = 0.0
        self._sweep_task: ScheduledTask | None = None
        self._prune_task: ScheduledTask | None = None
        self._sweeps = 0
        self._sweeps_refused = 0
        self._pruned = 0
        self._prune_sweeps = 0
        self._prune_refusals = 0
        self._last_prune_refusal = ""
        self._prune_last_run: float | None = None
        self._prune_last_removed = 0
        self._prune_reclaimed_bytes = 0

    def init(self, context: CoreContext) -> None:
        """Resolve scoped settings; paths fall back to chassis data directories.

        Counters describe the run the operator is looking at: a chassis that
        re-initializes this module starts counting again, and 'kdx doctor' never
        blames yesterday's refusals on today's copy.
        """
        super().init(context)
        self._runs = 0
        self._last = None
        self._refusals = 0
        self._last_refusal = ""
        self._sweeps = 0
        self._sweeps_refused = 0
        self._pruned = 0
        self._prune_sweeps = 0
        self._prune_refusals = 0
        self._last_prune_refusal = ""
        self._prune_last_run = None
        self._prune_last_removed = 0
        self._prune_reclaimed_bytes = 0
        default_source = str(context.paths.root)
        default_dest = str(context.paths.data_dir / "backups")
        self._source = Path(str(self.config.get_str("source", default_source)))
        self._dest = Path(str(self.config.get_str("dest", default_dest)))
        include = self.config.get_list("include", ["*"])
        exclude = self.config.get_list("exclude", list(DEFAULT_EXCLUDES))
        self._include = tuple(str(item) for item in include) or ("*",)
        self._exclude = tuple(str(item) for item in exclude)
        raw_max = self.config.get_str("max_size", "")
        self._max_bytes = parse_size(raw_max) if raw_max.strip() else None
        self._dry_run = bool(self.config.get_bool("dry_run", False))
        self._follow_symlinks = bool(self.config.get_bool("follow_symlinks", False))
        self._schedule_interval = max(0.0, float(self.config.get_float("schedule_interval", 0.0)))
        self._auto_prune_days = max(0, int(self.config.get_int("auto_prune_days", 0)))
        self._prune_interval = max(0.0, float(self.config.get_float("prune_interval", 0.0)))
        self.logger.debug(
            "backup_engine configured",
            source=str(self._source),
            dest=str(self._dest),
            max_bytes=self._max_bytes,
            dry_run=self._dry_run,
            schedule_interval=round(self._schedule_interval, 3),
            auto_prune_days=self._auto_prune_days,
            prune_interval=round(self._prune_interval, 3),
        )

    def config_schema(self) -> ConfigSchema:
        """Declare every scoped key this module reads, with the default it falls back to."""
        return ConfigSchema(
            module=self.name,
            fields=(
                ConfigField(
                    "source",
                    kind=ConfigKind.PATH,
                    default=".",
                    description="Tree to copy; the chassis default is the project root.",
                ),
                ConfigField(
                    "dest",
                    kind=ConfigKind.PATH,
                    default="backups",
                    description="Where timestamped copies land, under the data directory.",
                ),
                ConfigField(
                    "include",
                    kind=ConfigKind.LIST,
                    default=["*"],
                    description="Names a backup may copy.",
                ),
                ConfigField(
                    "exclude",
                    kind=ConfigKind.LIST,
                    default=list(DEFAULT_EXCLUDES),
                    description="Names a backup never copies.",
                ),
                ConfigField(
                    "max_size",
                    kind=ConfigKind.STR,
                    default="",
                    description="Refuse a source larger than this, e.g. '2G'; empty means none.",
                ),
                ConfigField(
                    "dry_run",
                    kind=ConfigKind.BOOL,
                    default=False,
                    description="Report the plan without copying anything.",
                ),
                ConfigField(
                    "follow_symlinks",
                    kind=ConfigKind.BOOL,
                    default=False,
                    description="Follow symlinks while walking the source tree.",
                ),
                ConfigField(
                    "schedule_interval",
                    kind=ConfigKind.FLOAT,
                    default=0.0,
                    description=(
                        "Seconds between automatic sweeps; 0 leaves copying to the operator."
                    ),
                ),
                ConfigField(
                    "auto_prune_days",
                    kind=ConfigKind.INT,
                    default=0,
                    description="Keep stamped copies for this many days; 0 keeps every copy.",
                ),
                ConfigField(
                    "prune_interval",
                    kind=ConfigKind.FLOAT,
                    default=0.0,
                    description=(
                        "Seconds between independent retention sweeps; 0 ages copies only "
                        "after a copy lands, and needs auto_prune_days above 0 to sweep at all."
                    ),
                ),
            ),
        )

    def diagnostics(self) -> tuple[DiagnosticCheck, ...]:
        """Report the source, the destination, and any run the lock refused."""
        if self.context is None:
            # The chassis reads this hook at registration too, before init() has
            # resolved the paths: there is nothing honest to report yet.
            return ()
        settings = self.settings()
        checks: list[DiagnosticCheck] = []
        source = settings.source
        if source.is_dir():
            checks.append(
                DiagnosticCheck(
                    name="source",
                    message=f"backup source {source} is a readable tree",
                    module=self.name,
                )
            )
        elif source.exists():
            checks.append(
                DiagnosticCheck(
                    name="source",
                    message=f"backup source {source} is not a directory",
                    severity=DiagnosticSeverity.ERROR,
                    hint="a backup copies a tree; point modules.backup_engine.source at one",
                    module=self.name,
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name="source",
                    message=f"backup source {source} does not exist",
                    severity=DiagnosticSeverity.WARNING,
                    hint="set modules.backup_engine.source, or let the default project root stand",
                    module=self.name,
                )
            )
        checks.append(self._destination_check(settings.dest))
        checks.append(self._sweep_check())
        checks.append(self._prune_check())
        if self._refusals:
            checks.append(self._refusal_check())
        return tuple(checks)

    def _refusal_check(self) -> DiagnosticCheck:
        """Report that a run had to step aside for a copy already in progress.

        The chassis does not ask for this check: it appears only once a run was refused,
        which is the operator's first sign that two schedulers are copying one tree.
        """
        return DiagnosticCheck(
            name="lock",
            message=(
                f"{self._refusals} backup run(s) stepped aside for the {self.LOCK_NAME} lock"
            ),
            severity=DiagnosticSeverity.WARNING,
            hint=self._last_refusal or "another copy is holding the run lock",
            module=self.name,
        )

    def _destination_check(self, dest: Path) -> DiagnosticCheck:
        """Report whether *dest* can receive a backup, or will be created on the first run."""
        if dest.is_dir():
            if os.access(dest, os.W_OK):
                return DiagnosticCheck(
                    name="destination",
                    message=f"backup destination {dest} is writable",
                    module=self.name,
                )
            return DiagnosticCheck(
                name="destination",
                message=f"backup destination {dest} is not writable",
                severity=DiagnosticSeverity.ERROR,
                hint="check the permissions there, or set modules.backup_engine.dest",
                module=self.name,
            )
        if dest.parent.is_dir():
            return DiagnosticCheck(
                name="destination",
                message=f"backup destination {dest} is created on the first run",
                module=self.name,
            )
        return DiagnosticCheck(
            name="destination",
            message=f"backup destination {dest} has no parent directory at {dest.parent}",
            severity=DiagnosticSeverity.WARNING,
            hint="create the parent, or set modules.backup_engine.dest",
            module=self.name,
        )

    def ipc_routes(self) -> tuple[IpcRoute, ...]:
        """Expose the plan the current settings produce and the retention record, read-only."""
        return (
            IpcRoute(
                path="/api/v1/backup/plan",
                handler=self._plan_route,
                module=self.name,
                summary="the backup plan the current settings produce",
            ),
            IpcRoute(
                path="/api/v1/backup/retention",
                handler=self._retention_route,
                module=self.name,
                summary="the retention policy, standing copies, and sweep record",
            ),
        )

    def _retention_route(self, request: IpcRequest) -> Any:
        """Answer the retention route from inside the running chassis, counters live."""
        return self.retention_report().as_dict()

    def _plan_route(self, request: IpcRequest) -> Any:
        """Answer the plan route; an unreadable source is a 503, not a stack trace."""
        settings = self.settings()
        try:
            plan = build_plan(settings)
        except BackupError as exc:
            hint = getattr(exc, "hint", "")
            return 503, {"error": str(exc), "hint": hint, "settings": settings.as_dict()}
        return {"settings": settings.as_dict(), "plan": plan.as_dict()}

    def register_cli(self, surface: CliSurface) -> None:
        """Contribute the 'backup' command group to the root surface."""
        surface.add_command(
            CliCommand(
                name="backup",
                help="Backup engine: plan, run, or configure a backup.",
                module_name=self.name,
                subcommands=(
                    CliCommand(
                        name="plan",
                        help="Validate inputs and show what a backup would copy.",
                        module_name=self.name,
                        handler=_plan_command,
                        args=_plan_arguments(),
                    ),
                    CliCommand(
                        name="run",
                        help="Run the backup (copies files unless --dry-run).",
                        module_name=self.name,
                        handler=_run_command,
                        args=(
                            *_plan_arguments(),
                            CliArgument(
                                ("--force",),
                                dest="force",
                                action="store_true",
                                help="Copy even when the size budget is exceeded.",
                            ),
                        ),
                    ),
                    CliCommand(
                        name="retention",
                        help="Report the retention window, standing copies, and sweep record.",
                        module_name=self.name,
                        handler=_retention_command,
                        args=(
                            CliArgument(
                                ("--json",),
                                dest="as_json",
                                action="store_true",
                                help="Emit the report as JSON.",
                            ),
                        ),
                    ),
                    CliCommand(
                        name="configure",
                        help="Interactive setup: source, destination, filters, then run.",
                        module_name=self.name,
                        handler=_configure_command,
                    ),
                ),
            )
        )

    def start(self) -> None:
        """Mark the module running, announce the configuration, and schedule the sweep."""
        super().start()
        settings = self.settings()
        self.bus.publish(
            CONFIGURED_EVENT,
            {"source": str(settings.source), "dest": str(settings.dest)},
            source=self.name,
        )
        self.logger.info(
            f"{TAG_OK} backup engine ready",
            source=str(settings.source),
            dest=str(settings.dest),
            mode="dry run" if settings.dry_run else "copy",
        )
        sweep = self._start_sweep()
        if sweep is not None:
            self.logger.info(
                f"{TAG_OK} backup sweep scheduled",
                task=SWEEP_TASK,
                interval_seconds=round(self._schedule_interval, 3),
                auto_prune_days=self._auto_prune_days,
            )
        prune = self._start_prune()
        if prune is not None:
            self.logger.info(
                f"{TAG_OK} backup retention sweep scheduled",
                task=PRUNE_TASK,
                interval_seconds=round(self._prune_interval, 3),
                auto_prune_days=self._auto_prune_days,
            )

    def stop(self) -> None:
        """Withdraw both tasks and report the counters; nothing stays open between runs."""
        self._stop_sweep()
        self._stop_prune()
        super().stop()
        self.logger.info(
            f"{TAG_NOTE} backup engine stopped",
            runs=self._runs,
            sweeps=self._sweeps,
            prune_sweeps=self._prune_sweeps,
            pruned=self._pruned,
            reclaimed_bytes=self._prune_reclaimed_bytes,
            last_files=self._last.copied if self._last else 0,
        )

    def settings(self, **overrides: Any) -> BackupSettings:
        """Return the resolved settings with optional per-command overrides."""
        clean = {key: value for key, value in overrides.items() if value not in (None, "")}
        return BackupSettings(
            source=Path(str(clean.get("source", self._source))),
            dest=Path(str(clean.get("dest", self._dest))),
            include=tuple(clean["include"]) if clean.get("include") else self._include,
            exclude=tuple(clean["exclude"]) if clean.get("exclude") else self._exclude,
            max_bytes=clean.get("max_bytes", self._max_bytes),
            dry_run=bool(clean.get("dry_run", self._dry_run)),
            follow_symlinks=bool(clean.get("follow_symlinks", self._follow_symlinks)),
            stamp=str(clean.get("stamp", "")) or default_stamp(),
        )

    def plan(self, **overrides: Any) -> BackupPlan:
        """Build a plan from the resolved settings and overrides."""
        return build_plan(self.settings(**overrides))

    def run_backup(self, plan: BackupPlan, *, dry_run: bool | None = None) -> BackupResult:
        """Execute a plan under the run lock and remember the counters.

        The lock is the chassis primitive, not a private file: it is the same name
        `kdx doctor` reports, so an operator sees who is copying before asking why a
        second run was refused.
        """
        handle = self._acquire_run_lock()
        try:
            result = execute_plan(plan, dry_run=dry_run)
        finally:
            self._release_run_lock(handle)
        self._runs += 1
        self._last = result
        if not result.dry_run:
            report = self._prune_dest(plan.settings.dest, keep=plan.target)
            self._pruned += report.removed
            self._prune_reclaimed_bytes += report.reclaimed_bytes
        self.bus.publish(
            COMPLETED_EVENT,
            {"target": str(plan.target), "copied": result.copied, "dry_run": result.dry_run},
            source=self.name,
        )
        return result

    def _acquire_run_lock(self) -> NamedLock | None:
        """Take the run lock; raise a polite error when another copy holds it.

        The module takes its own advisory lock on the lock file instead of borrowing a
        chassis handle: a registry hands the same handle back to a process that already
        holds the name, which would let a scheduled copy and a manual one write the same
        destination at the same moment. None means there is no chassis to coordinate with -
        a unit test driving `execute_plan()` directly - not a lock the module skipped.
        """
        context = self.context
        if context is None:
            return None
        try:
            return context.paths.lock(
                self.LOCK_NAME,
                timeout=0.0,
                metadata={"module": self.name, "job": "backup run"},
                logger=self.logger,
            )
        except LockTimeoutError as exc:
            self._refusals += 1
            self._last_refusal = str(exc)
            raise BackupError(
                f"another backup is running: named lock {self.LOCK_NAME!r} is busy",
                hint="wait for the copy in progress, or ask 'kdx doctor' who holds it",
            ) from exc

    def _release_run_lock(self, handle: NamedLock | None) -> None:
        """Give the run lock back; releasing a lock this run never took is a no-op.

        `NamedLock.release()` is idempotent, and a payload write that failed already
        released the lock inside `acquire()`, so this only ever hands back a lock this run
        owned - no reservation is left behind in the chassis registry either.
        """
        if handle is not None:
            handle.release()

    def health(self) -> dict[str, Any]:
        """Return module counters for 'kdx modules show'."""
        base = super().health()
        base.update(
            {
                "runs": self._runs,
                "source": str(self._source),
                "dest": str(self._dest),
                "last_copied": self._last.copied if self._last else 0,
                "refusals": self._refusals,
                "sweep_interval_seconds": round(self._schedule_interval, 3),
                "sweep_scheduled": self._sweep_task is not None,
                "sweeps": self._sweeps,
                "sweeps_refused": self._sweeps_refused,
                "sweep_first_due_seconds": (
                    None
                    if self._sweep_task is None
                    else round(max(0.0, self._sweep_task.next_due - time.monotonic()), 3)
                ),
                "auto_prune_days": self._auto_prune_days,
                "pruned": self._pruned,
                "prune_interval_seconds": round(self._prune_interval, 3),
                "prune_scheduled": self._prune_task is not None,
                "prune_sweeps": self._prune_sweeps,
                "prune_sweeps_refused": self._prune_refusals,
                "prune_first_due_seconds": (
                    None
                    if self._prune_task is None
                    else round(max(0.0, self._prune_task.next_due - time.monotonic()), 3)
                ),
                "prune_last_run_seconds_ago": (
                    None
                    if self._prune_last_run is None
                    else round(max(0.0, time.time() - self._prune_last_run), 3)
                ),
                "prune_last_removed": self._prune_last_removed,
                "prune_reclaimed_bytes": self._prune_reclaimed_bytes,
                "prune_reclaimed": format_bytes(self._prune_reclaimed_bytes),
            }
        )
        return base


    def _start_sweep(self) -> ScheduledTask | None:
        """Hand the periodic sweep to the chassis clock, when one is configured.

        A sweep is a task, not a thread: the chassis clock decides when it is due, and the
        copy it starts takes the same named lock a manual 'kdx backup run' takes, so the two
        primitives are proven together. The first sweep lands one interval out, so booting
        never opens with a copy.
        """
        context = self.context
        if context is None or self._schedule_interval <= 0.0:
            return None
        task = context.schedule(
            interval_seconds=self._schedule_interval,
            callback=self._sweep_backup,
            name=SWEEP_TASK,
            module=self.name,
        )
        self._sweep_task = task
        return task

    def _stop_sweep(self) -> None:
        """Withdraw the sweep: a stopped module leaves no task on the chassis clock."""
        task = self._sweep_task
        self._sweep_task = None
        scheduler = self.scheduler if self.context is not None else None
        if task is not None and scheduler is not None:
            scheduler.unschedule(task.name)

    def _start_prune(self) -> ScheduledTask | None:
        """Hand the retention sweep to the chassis clock, when one is configured.

        Retention is a maintenance task in its own right, not a side effect of a copy:
        a project that copies weekly can still shed archives daily, and a manual 'kdx
        backup run' never has to prune what an older run left behind. It is declared
        only when there is a window to age out - a sweep with nothing to remove is a
        task spending the clock's attention on nothing.
        """
        context = self.context
        if context is None or self._prune_interval <= 0.0 or self._auto_prune_days <= 0:
            return None
        task = context.schedule(
            interval_seconds=self._prune_interval,
            callback=self._prune_sweep,
            name=PRUNE_TASK,
            module=self.name,
        )
        self._prune_task = task
        return task

    def _stop_prune(self) -> None:
        """Withdraw the retention sweep: a stopped module leaves no task on the clock."""
        task = self._prune_task
        self._prune_task = None
        scheduler = self.scheduler if self.context is not None else None
        if task is not None and scheduler is not None:
            scheduler.unschedule(task.name)

    def _acquire_prune_lock(self) -> NamedLock | None:
        """Take the maintenance lock; None when there is no chassis to coordinate with.

        Pruning shares the copy lock instead of inventing a second one: an archive is
        never deleted beside a copy that is still landing, and 'kdx doctor' reports one
        busy name whoever is holding it.
        """
        context = self.context
        if context is None:
            return None
        return context.paths.lock(
            self.LOCK_NAME,
            timeout=0.0,
            metadata={"module": self.name, "job": "backup prune"},
            logger=self.logger,
        )

    def _prune_sweep(self) -> None:
        """Age expired copies out on the clock, under the run lock.

        The sweep belongs to no backup: it runs on its own interval, so infrequent and
        manual copies cannot strand archives in the destination. A lock that is busy is
        a decision it honours - counted and logged rather than reported as a failing
        task - because the copy holding the lock is close to finishing and the next beat
        is only an interval away.
        """
        try:
            handle = self._acquire_prune_lock()
        except LockTimeoutError as exc:
            self._prune_refusals += 1
            self._last_prune_refusal = str(exc)
            self.logger.info(
                f"{TAG_NOTE} backup retention stepped aside",
                reason=str(exc),
                interval_seconds=round(self._prune_interval, 3),
            )
            return
        try:
            report = self._prune_dest(self._dest)
        finally:
            self._release_run_lock(handle)
        self._prune_sweeps += 1
        self._prune_last_run = time.time()
        self._prune_last_removed = report.removed
        self._pruned += report.removed
        self._prune_reclaimed_bytes += report.reclaimed_bytes
        self.logger.info(
            f"{TAG_OK} backup retention swept",
            dest=str(self._dest),
            scanned=report.scanned,
            removed=report.removed,
            reclaimed=format_bytes(report.reclaimed_bytes),
            auto_prune_days=self._auto_prune_days,
        )

    def _sweep_backup(self) -> None:
        """Copy the tree on the clock, stepping aside for a copy already running.

        This is the pairing of two chassis primitives: the clock decides when work is due,
        and the named lock decides whether this process may copy. A refusal is a decision the
        sweep honours, so it is counted and logged rather than reported as a failing task.
        Any other backup error - an unreadable source, a budget exceeded - is left to rise,
        because the scheduler records it and 'kdx doctor' says which task keeps failing.
        """
        plan = self.plan()
        refusals = self._refusals
        try:
            result = self.run_backup(plan)
        except BackupError as exc:
            if self._refusals == refusals:
                raise
            self._sweeps_refused += 1
            self.logger.info(
                f"{TAG_NOTE} backup sweep stepped aside",
                reason=str(exc),
                interval_seconds=round(self._schedule_interval, 3),
            )
            return
        self._sweeps += 1
        self.logger.info(
            f"{TAG_OK} backup sweep copied",
            target=str(result.plan.target),
            files=result.copied,
            bytes_copied=result.bytes_copied,
        )

    @staticmethod
    def _stamp_time(label: str) -> float | None:
        """Return the moment *label* names, when it is a stamp this chassis would write."""
        try:
            parsed = datetime.strptime(label, STAMP_FORMAT)
        except ValueError:
            return None
        return parsed.timestamp()

    def _prune_dest(self, dest: Path, *, keep: Path | None = None) -> PruneReport:
        """Remove stamped copies older than 'auto_prune_days' under *dest* and report it.

        Only a directory directly inside the destination whose own name is a backup stamp is
        a candidate, so a folder the operator put there by hand is never deleted, and the
        copy a run just made is kept by naming it in *keep*. A copy that would not delete
        (a permissions problem, a file still open) is reported and stays; the sweep moves
        on rather than failing a maintenance pass over one folder.
        """
        report = PruneReport()
        days = self._auto_prune_days
        if days <= 0 or not dest.is_dir():
            return report
        cutoff = time.time() - days * SECONDS_PER_DAY
        for child in sorted(dest.iterdir()):
            if not child.is_dir() or child == keep:
                continue
            stamp = self._stamp_time(child.name)
            if stamp is None or stamp >= cutoff:
                continue
            report.scanned += 1
            freed = _tree_bytes(child)
            try:
                shutil.rmtree(child)
            except OSError as exc:
                self.logger.warning(
                    f"{TAG_NOTE} backup copy stayed", copy=str(child), reason=str(exc)
                )
                continue
            report.removed += 1
            report.reclaimed_bytes += freed
        if report.removed:
            self.bus.publish(
                PRUNED_EVENT,
                {
                    "days": days,
                    "removed": report.removed,
                    "reclaimed_bytes": report.reclaimed_bytes,
                },
                source=self.name,
            )
        return report

    def _prune_old_copies(self, dest: Path, *, keep: Path | None = None) -> int:
        """Remove expired copies under *dest* and return how many went; 0 keeps every copy."""
        return self._prune_dest(dest, keep=keep).removed

    def retention_report(self) -> RetentionReport:
        """Answer 'what has the retention policy done?' in one honest pass.

        The policy comes from the configuration, the standing copies from the
        destination itself (only folders whose own name is a backup stamp count,
        the same rule a sweep deletes by), and the sweep counters from this
        process. A copy that stands past the window is reported, not removed:
        this is the report, not the sweep.
        """
        report = RetentionReport(
            dest=str(self._dest),
            window_days=self._auto_prune_days,
            sweep_interval_seconds=round(self._prune_interval, 3),
            sweep_scheduled=self._prune_task is not None,
            sweeps=self._prune_sweeps,
            last_sweep_seconds_ago=(
                None
                if self._prune_last_run is None
                else round(max(0.0, time.time() - self._prune_last_run), 3)
            ),
            last_sweep_removed=self._prune_last_removed,
            removed_total=self._pruned,
            reclaimed_bytes=self._prune_reclaimed_bytes,
            refusals=self._prune_refusals,
        )
        dest = self._dest
        if dest.is_dir():
            cutoff = (
                time.time() - self._auto_prune_days * SECONDS_PER_DAY
                if self._auto_prune_days > 0
                else None
            )
            oldest: float | None = None
            newest: float | None = None
            for child in sorted(dest.iterdir()):
                if not child.is_dir():
                    continue
                stamp = self._stamp_time(child.name)
                if stamp is None:
                    continue
                report.copies_standing += 1
                report.copies_bytes += _tree_bytes(child)
                if cutoff is not None and stamp < cutoff:
                    report.expired_now += 1
                if oldest is None or stamp < oldest:
                    oldest = stamp
                if newest is None or stamp > newest:
                    newest = stamp
            if oldest is not None and newest is not None:
                report.oldest_stamp = datetime.fromtimestamp(oldest).strftime(STAMP_FORMAT)
                report.newest_stamp = datetime.fromtimestamp(newest).strftime(STAMP_FORMAT)
        return report

    def _prune_check(self) -> DiagnosticCheck:
        """Report the independent retention sweep, its last beat, and what it reclaimed.

        The honest states are the ones an operator can act on: retention left to the copy,
        a sweep with no window to age out, no clock to run on, a task that was withdrawn,
        and a sweep that is alive and saying how much space it took back.
        """
        every = round(self._prune_interval, 3)
        if self._prune_interval <= 0.0:
            return DiagnosticCheck(
                name="retention",
                message="retention ages copies out only as a copy lands",
                hint="set modules.backup_engine.prune_interval to sweep retention on the clock",
                module=self.name,
            )
        if self._auto_prune_days <= 0:
            return DiagnosticCheck(
                name="retention",
                message=f"retention sweep every {every} s has no window to age out",
                severity=DiagnosticSeverity.WARNING,
                hint="set modules.backup_engine.auto_prune_days above 0, or prune_interval at 0",
                module=self.name,
            )
        task = self._prune_task
        if task is None:
            scheduler = self.context.scheduler if self.context is not None else None
            if scheduler is None:
                return DiagnosticCheck(
                    name="retention",
                    message=f"retention sweep every {every} s has no clock to run on",
                    severity=DiagnosticSeverity.WARNING,
                    hint="set core.scheduler.enabled to true, or leave prune_interval at 0",
                    module=self.name,
                )
            if not scheduler.running:
                # 'kdx doctor' initializes the modules it inspects and never starts them:
                # the clock exists, the task simply was never declared, and blaming the
                # chassis for that would be a warning an operator cannot act on.
                return DiagnosticCheck(
                    name="retention",
                    message=f"retention sweep every {every} s waits for the chassis to start",
                    hint="the module declares its maintenance task in start(); boot it to run",
                    module=self.name,
                )
            return DiagnosticCheck(
                name="retention",
                message=f"retention sweep every {every} s declared no task on a running clock",
                severity=DiagnosticSeverity.WARNING,
                hint="restart the module to declare the sweep again",
                module=self.name,
            )
        scheduler = self.context.scheduler if self.context is not None else None
        live = scheduler is not None and scheduler.task(task.name) is task
        if not live:
            return DiagnosticCheck(
                name="retention",
                message=f"retention task {task.name} is gone",
                severity=DiagnosticSeverity.WARNING,
                hint="a scheduled task was withdrawn; restart the module to declare it again",
                module=self.name,
            )
        last = (
            "never run"
            if self._prune_last_run is None
            else f"last run {round(max(0.0, time.time() - self._prune_last_run), 1)} s ago"
        )
        reclaimed = format_bytes(self._prune_reclaimed_bytes)
        summary = (
            f"auto_prune ages copies every {every} s ({self._prune_sweeps} swept, "
            f"{self._pruned} removed, {reclaimed} reclaimed, {last})"
        )
        if self._prune_refusals and not self._prune_sweeps:
            return DiagnosticCheck(
                name="retention",
                message=f"{summary}, {self._prune_refusals} stepped aside",
                severity=DiagnosticSeverity.WARNING,
                hint=self._last_prune_refusal or "a copy is holding the maintenance lock",
                module=self.name,
            )
        return DiagnosticCheck(name="retention", message=summary, module=self.name)

    def _sweep_check(self) -> DiagnosticCheck:
        """Report the periodic sweep the chassis clock runs, and whether it is still there."""
        every = round(self._schedule_interval, 3)
        if self._schedule_interval <= 0.0:
            return DiagnosticCheck(
                name="sweep",
                message="automatic sweep is left to the operator",
                hint="set modules.backup_engine.schedule_interval to copy on the clock",
                module=self.name,
            )
        task = self._sweep_task
        if task is None:
            if self.context is None:
                return DiagnosticCheck(
                    name="sweep",
                    message=f"automatic sweep every {every} s waits for a chassis",
                    module=self.name,
                )
            return DiagnosticCheck(
                name="sweep",
                message=f"automatic sweep every {every} s has no clock to run on",
                severity=DiagnosticSeverity.WARNING,
                hint="set core.scheduler.enabled to true, or leave schedule_interval at 0",
                module=self.name,
            )
        scheduler = self.context.scheduler if self.context is not None else None
        live = scheduler is not None and scheduler.task(task.name) is task
        if not live:
            return DiagnosticCheck(
                name="sweep",
                message=f"automatic sweep task {task.name} is gone",
                severity=DiagnosticSeverity.WARNING,
                hint="a scheduled task was withdrawn; restart the module to declare it again",
                module=self.name,
            )
        return DiagnosticCheck(
            name="sweep",
            message=f"auto_sweep copies every {every} s ({self._sweeps} copied, "
            f"{self._sweeps_refused} stepped aside)",
            module=self.name,
        )


def _tree_bytes(directory: Path) -> int:
    """Return the bytes held under *directory*, best effort, measured before it is removed.

    A retention sweep claims the sizes of the files it deleted as reclaimed space: that is
    what the operator can check against the folder that thinned, not the filesystem's free
    space, which any other process can move underneath the measurement.
    """
    total = 0
    for root, _dirs, names in os.walk(directory):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _plan_arguments() -> tuple[CliArgument, ...]:
    return (
        CliArgument(
            ("--source",), dest="source", metavar="DIR", help="Source directory to back up."
        ),
        CliArgument(
            ("--dest",), dest="dest", metavar="DIR", help="Destination directory for copies."
        ),
        CliArgument(
            ("--include",),
            dest="include",
            metavar="PATTERN",
            action="append",
            help="Only copy paths matching PATTERN (repeatable).",
        ),
        CliArgument(
            ("--exclude",),
            dest="exclude",
            metavar="PATTERN",
            action="append",
            help="Skip paths matching PATTERN (repeatable).",
        ),
        CliArgument(
            ("--max-size",),
            dest="max_size",
            metavar="SIZE",
            help="Fail if the copy exceeds SIZE, e.g. 500MB.",
        ),
        CliArgument(
            ("--dry-run",),
            dest="dry_run",
            action="store_true",
            help="Report what would be copied without writing.",
        ),
        CliArgument(
            ("--json",), dest="as_json", action="store_true", help="Emit the report as JSON."
        ),
    )


def _module(request: CliRequest) -> BackupEngineModule:
    registry = request.context.registry
    if registry is None:
        raise RuntimeError("the backup command needs the module registry")
    return cast(BackupEngineModule, registry.require("backup_engine").module)


def _overrides(request: CliRequest) -> dict[str, Any]:
    raw_max = request.option("max_size")
    max_bytes: int | None = None
    if raw_max:
        try:
            max_bytes = parse_size(str(raw_max))
        except ValueError as exc:
            raise BackupError(str(exc), hint="Use a size like 500MB or 1.5GiB.") from exc
    include = request.option("include") or None
    exclude = request.option("exclude") or None
    return {
        "source": request.option("source"),
        "dest": request.option("dest"),
        "include": list(include) if include else None,
        "exclude": list(exclude) if exclude else None,
        "max_bytes": max_bytes,
        "dry_run": True if request.flag("dry_run") else None,
    }


def _plan_command(request: CliRequest) -> int:
    module = _module(request)
    try:
        plan = module.plan(**_overrides(request))
    except BackupError as exc:
        write_status(str(exc), tag=TAG_ERROR)
        if exc.hint:
            write_status(exc.hint, tag=TAG_NOTE)
        return int(ExitCode.ERROR)
    if request.flag("as_json"):
        dump_json(plan.as_dict())
        return int(ExitCode.SUCCESS)
    print_section("Backup plan")
    print_key_values(describe_plan(plan))
    write_status("nothing copied yet: this was a plan", tag=TAG_NOTE)
    return int(ExitCode.SUCCESS)


def _run_command(request: CliRequest) -> int:
    module = _module(request)
    overrides = _overrides(request)
    force = bool(request.flag("force"))
    try:
        plan = module.plan(**overrides)
    except BackupError as exc:
        if not force:
            write_status(str(exc), tag=TAG_ERROR)
            write_status("pass --force to copy anyway", tag=TAG_NOTE)
            return int(ExitCode.ERROR)
        write_status(str(exc), tag=TAG_NOTE)
        plan = build_plan(replace(module.settings(**overrides), max_bytes=None))
    try:
        result = module.run_backup(plan, dry_run=overrides.get("dry_run"))
    except BackupError as exc:
        # A run the lock refused is news the operator can act on, not a crash.
        if request.flag("as_json"):
            dump_json({"error": exc.message, "hint": exc.hint})
        else:
            write_status(str(exc), tag=TAG_ERROR)
        return int(ExitCode.ERROR)
    if request.flag("as_json"):
        dump_json(result.as_dict())
        return int(ExitCode.SUCCESS)
    print_section("Backup result")
    print_key_values(describe_result(result))
    tag = TAG_NOTE if result.dry_run else TAG_OK
    write_status(
        f"{result.copied} file(s) {'would be' if result.dry_run else ''} copied".replace("  ", " "),
        tag=tag,
    )
    return int(ExitCode.SUCCESS)


def describe_retention(report: RetentionReport) -> list[tuple[str, Any]]:
    """Render the retention report as operator rows."""
    if report.window_days > 0:
        window = f"{report.window_days} day(s)"
    else:
        window = "off (copies age out only as copies land)"
    if report.sweep_interval_seconds > 0:
        sweep = f"every {report.sweep_interval_seconds} s"
        sweep += " (on the clock)" if report.sweep_scheduled else " (no clock yet)"
    else:
        sweep = "only as a copy lands"
    rows: list[tuple[str, Any]] = [
        ("destination", report.dest),
        ("window", window),
        ("sweep", sweep),
        ("copies standing", f"{report.copies_standing} ({format_bytes(report.copies_bytes)})"),
        ("oldest / newest", f"{report.oldest_stamp or '-'} / {report.newest_stamp or '-'}"),
        ("past the window now", report.expired_now),
        ("sweeps run", report.sweeps),
        (
            "last sweep",
            "never"
            if report.last_sweep_seconds_ago is None
            else f"{report.last_sweep_seconds_ago} s ago ({report.last_sweep_removed} removed)",
        ),
        ("removed in total", report.removed_total),
        ("reclaimed", format_bytes(report.reclaimed_bytes)),
    ]
    if report.refusals:
        rows.append(("sweeps stepped aside", report.refusals))
    return rows


def _retention_command(request: CliRequest) -> int:
    module = _module(request)
    report = module.retention_report()
    if request.flag("as_json"):
        dump_json(report.as_dict())
        return int(ExitCode.SUCCESS)
    print_section("Backup retention")
    print_key_values(describe_retention(report))
    if report.window_days <= 0:
        write_status(
            "the window is off: copies stand until the window opens or a sweep is asked for",
            tag=TAG_NOTE,
        )
    elif report.expired_now:
        write_status(
            f"{report.expired_now} copy(ies) stand past the window; the next sweep removes them",
            tag=TAG_NOTE,
        )
    else:
        write_status("every standing copy is inside the window", tag=TAG_OK)
    return int(ExitCode.SUCCESS)


def _configure_command(request: CliRequest) -> int:
    """Interactive configuration with validation, then run the chosen mode."""
    from ...cli.ux import UserExperience
    from ...validate import require_size

    module = _module(request)
    ux = UserExperience(request.context.config)
    base = module.settings()
    source = ux.ask_path("Source directory", default=str(base.source), must_exist=True, kind="dir")
    dest = ux.ask_path("Destination directory", default=str(base.dest), writable=True)
    include = ux.ask_text("Include pattern", default=", ".join(base.include), example="*.txt")
    exclude = ux.ask_text(
        "Exclude patterns",
        default=", ".join(base.exclude),
        example=".git, __pycache__",
    )
    raw_max = ux.ask_text("Size budget", default="", validator=require_size, example="500MB")
    try:
        budget = parse_size(str(raw_max)) if str(raw_max).strip() else None
    except ValueError:
        budget = None
    dry_run = ux.confirm("Dry run (no files written)", default=True)
    settings = module.settings(
        source=str(source),
        dest=str(dest),
        include=tuple(part.strip() for part in include.split(",") if part.strip()),
        exclude=tuple(part.strip() for part in exclude.split(",") if part.strip()),
        max_bytes=budget,
        dry_run=dry_run,
    )
    plan = build_plan(settings)
    print_section("Backup plan")
    print_key_values(describe_plan(plan))
    if not ux.confirm("Run this backup now", default=not dry_run):
        write_status("configured, but not run", tag=TAG_NOTE)
        return int(ExitCode.SUCCESS)
    try:
        result = module.run_backup(plan)
    except BackupError as exc:
        write_status(str(exc), tag=TAG_ERROR)
        return int(ExitCode.ERROR)
    print_section("Backup result")
    print_key_values(describe_result(result))
    return int(ExitCode.SUCCESS)


MODULE = BackupEngineModule()
