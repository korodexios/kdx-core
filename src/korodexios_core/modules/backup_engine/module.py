"""backup_engine: configurable source-to-destination backups with a dry run."""

from __future__ import annotations

import os
from dataclasses import replace
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
from ...validate import parse_size
from .engine import (
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

    def init(self, context: CoreContext) -> None:
        """Resolve scoped settings; paths fall back to chassis data directories."""
        super().init(context)
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
        self.logger.debug(
            "backup_engine configured",
            source=str(self._source),
            dest=str(self._dest),
            max_bytes=self._max_bytes,
            dry_run=self._dry_run,
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
        """Expose the plan the current settings produce, read-only."""
        return (
            IpcRoute(
                path="/api/v1/backup/plan",
                handler=self._plan_route,
                module=self.name,
                summary="the backup plan the current settings produce",
            ),
        )

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
                        name="configure",
                        help="Interactive setup: source, destination, filters, then run.",
                        module_name=self.name,
                        handler=_configure_command,
                    ),
                ),
            )
        )

    def start(self) -> None:
        """Mark the module running and announce the resolved configuration."""
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

    def stop(self) -> None:
        """Mark the module stopped; no files or handles stay open between runs."""
        super().stop()
        self.logger.info(
            f"{TAG_NOTE} backup engine stopped",
            runs=self._runs,
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
            }
        )
        return base


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
