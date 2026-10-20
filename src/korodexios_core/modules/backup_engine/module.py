"""backup_engine: configurable source-to-destination backups with a dry run."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from ...cli.render import (
    dump_json,
    print_key_values,
    print_section,
    write_status,
)
from ...constants import TAG_ERROR, TAG_NOTE, TAG_OK, ExitCode
from ...kernel.cli_contract import CliArgument, CliCommand, CliRequest, CliSurface
from ...kernel.context import CoreContext
from ...kernel.contract import Module
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
        """Announce the resolved configuration on the event bus."""
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
        """Report counters; no files or handles stay open between runs."""
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
        """Execute a plan and remember the counters."""
        result = execute_plan(plan, dry_run=dry_run)
        self._runs += 1
        self._last = result
        self.bus.publish(
            COMPLETED_EVENT,
            {"target": str(plan.target), "copied": result.copied, "dry_run": result.dry_run},
            source=self.name,
        )
        return result

    def health(self) -> dict[str, Any]:
        """Return module counters for 'korodex modules show'."""
        base = super().health()
        base.update(
            {
                "runs": self._runs,
                "source": str(self._source),
                "dest": str(self._dest),
                "last_copied": self._last.copied if self._last else 0,
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
    result = module.run_backup(plan, dry_run=overrides.get("dry_run"))
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
    result = module.run_backup(plan)
    print_section("Backup result")
    print_key_values(describe_result(result))
    return int(ExitCode.SUCCESS)


MODULE = BackupEngineModule()
