"""backup_engine tests: planning, filters, budgets, dry runs, and the CLI surface."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.kernel.cli_contract import CliCommand
from korodexios_core.kernel.diagnostics import DiagnosticSeverity
from korodexios_core.modules.backup_engine import (
    MODULE,
    BackupEngineModule,
    BackupError,
    BackupSettings,
    build_plan,
    execute_plan,
)
from korodexios_core.modules.backup_engine.engine import STAMP_FORMAT, default_stamp
from korodexios_core.paths import AppPaths
from tests.test_chassis import build_chassis

EXCLUDES = (".git", "__pycache__", "*.log")


class RecordingSurface:
    """Minimal CliSurface double: records what a module contributes."""

    def __init__(self) -> None:
        self.added: list[CliCommand] = []

    def add_command(self, command: CliCommand) -> None:
        self.added.append(command)


@pytest.fixture()
def source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (source / "nested" / "beta.txt").write_text("beta contents\n", encoding="utf-8")
    (source / "notes.log").write_text("noise\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "cached.pyc").write_text("junk\n", encoding="utf-8")
    return source


def base_args(project_root) -> list[str]:
    return ["--home", str(project_root), "--no-log-file", "--no-color"]


def settings_for(source: Path, dest: Path, **overrides: object) -> BackupSettings:
    return BackupSettings(
        source=source,
        dest=dest,
        exclude=EXCLUDES,
        **overrides,  # type: ignore[arg-type]
    )


def test_build_plan_honours_excludes(source_tree: Path, tmp_path: Path) -> None:
    plan = build_plan(settings_for(source_tree, tmp_path / "dest"))
    names = sorted(str(path.relative_to(source_tree)) for path in plan.files)
    assert names == ["alpha.txt", "nested/beta.txt"]
    assert plan.total_bytes == 6 + 14
    assert plan.target.parent == tmp_path / "dest"


def test_build_plan_include_filter(source_tree: Path, tmp_path: Path) -> None:
    plan = build_plan(settings_for(source_tree, tmp_path / "dest", include=("*.txt",)))
    assert sorted(path.name for path in plan.files) == ["alpha.txt", "beta.txt"]


def test_build_plan_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="does not exist"):
        build_plan(settings_for(tmp_path / "nowhere", tmp_path / "dest"))


def test_build_plan_rejects_file_source(tmp_path: Path) -> None:
    a_file = tmp_path / "plain.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(BackupError, match="not a directory"):
        build_plan(settings_for(a_file, tmp_path / "dest"))


def test_size_budget_is_enforced(source_tree: Path, tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="over the 3 byte limit"):
        build_plan(settings_for(source_tree, tmp_path / "dest", max_bytes=3))


def test_dry_run_writes_nothing(source_tree: Path, tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    plan = build_plan(settings_for(source_tree, dest, stamp="run-1"))
    result = execute_plan(plan, dry_run=True)
    assert result.copied == 2
    assert result.bytes_copied == 20
    assert not dest.exists()


def test_execute_copies_tree_and_preserves_contents(source_tree: Path, tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    plan = build_plan(settings_for(source_tree, dest, stamp="run-2"))
    result = execute_plan(plan, dry_run=False)
    assert result.copied == 2 and not result.errors
    copied = dest / "run-2" / "nested" / "beta.txt"
    assert copied.read_text(encoding="utf-8") == "beta contents\n"


def test_module_lifecycle_publishes_events(project_root) -> None:
    chassis = build_chassis(project_root)
    chassis.initialize()
    chassis.start()
    module = chassis.registry.require("backup_engine").module
    assert isinstance(module, BackupEngineModule)
    events = [event.name for event in chassis.bus.history()]
    assert "backup.configured" in events
    chassis.shutdown()
    assert module.health()["runs"] == 0


def test_cli_backup_plan_json(capsys, project_root, source_tree: Path) -> None:
    args = [
        *base_args(project_root),
        "backup",
        "plan",
        "--source",
        str(source_tree),
        "--dest",
        str(project_root / "backups"),
        "--json",
    ]
    assert main(args) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"] == 2
    assert payload["total_bytes"] == 20


def test_cli_backup_run_dry_then_real(capsys, project_root, source_tree: Path) -> None:
    dest = project_root / "backups"
    dry = [
        *base_args(project_root),
        "backup",
        "run",
        "--source",
        str(source_tree),
        "--dest",
        str(dest),
        "--dry-run",
        "--json",
    ]
    assert main(dry) == int(ExitCode.SUCCESS)
    dry_payload = json.loads(capsys.readouterr().out)
    assert dry_payload["dry_run"] is True
    assert not list(dest.glob("*/alpha.txt"))

    real = [
        *base_args(project_root),
        "backup",
        "run",
        "--source",
        str(source_tree),
        "--dest",
        str(dest),
        "--json",
    ]
    assert main(real) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    assert payload["copied"] == 2
    assert list(dest.glob("*/alpha.txt"))


def test_cli_backup_plan_reports_missing_source(capsys, project_root) -> None:
    args = [
        *base_args(project_root),
        "backup",
        "plan",
        "--source",
        str(project_root / "nope"),
        "--json",
    ]
    assert main(args) == int(ExitCode.ERROR)
    assert "[!]" in capsys.readouterr().out


def test_shared_module_instance() -> None:
    assert isinstance(MODULE, BackupEngineModule)
    assert MODULE.name == "backup_engine"


def test_destination_inside_source_is_auto_excluded(tmp_path: Path) -> None:
    source = tmp_path / "project"
    (source / "src").mkdir(parents=True)
    (source / "src" / "a.txt").write_text("a", encoding="utf-8")
    dest = source / "backups"
    plan = build_plan(
        BackupSettings(source=source, dest=dest, include=("*",), exclude=()),
        stamp="run-1",
    )
    assert plan.auto_excluded == ["backups"]
    assert sorted(str(item.relative_to(source)) for item in plan.files) == ["src/a.txt"]
    assert plan.as_dict()["auto_excluded"] == ["backups"]


def test_symlink_cycles_are_pruned(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("creating directory symlinks needs privileges on Windows")
    source = tmp_path / "loop"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    os.symlink(str(source), str(source / "self"))
    plan = build_plan(
        BackupSettings(
            source=source, dest=tmp_path / "out", include=("*",), exclude=(), follow_symlinks=True
        ),
        stamp="run-1",
    )
    assert plan.skipped == ["self (symlink cycle)"]
    assert [item.name for item in plan.files] == ["a.txt"]


def backup_lock_chassis(tmp_path: Path) -> Chassis:
    """Return a running chassis whose backup_engine copies one small tree."""
    root = tmp_path / "project"
    source = root / "src"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("alpha\n", encoding="utf-8")
    paths = AppPaths.discover(root=str(root), environ={})
    chassis = Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            install_signals=False,
            cli_overrides={"core.log.file_enabled": False, "core.ipc.enabled": False},
        )
    )
    chassis.initialize()
    chassis.start(ipc=False)
    return chassis


def test_a_second_run_steps_aside_for_the_named_lock(tmp_path: Path) -> None:
    """The module takes the chassis named lock, so two copies never share one destination."""
    chassis = backup_lock_chassis(tmp_path)
    try:
        module = chassis.registry.require("backup_engine").module
        refusals = module.health()["refusals"]
        assert chassis.locks.names() == (), "no lock is taken before a run"
        assert module.run_backup(module.plan()).copied >= 1
        assert chassis.locks.names() == (), "a finished run releases the name it took"
        with chassis.lock("backup_engine", timeout=0.0), pytest.raises(BackupError) as captured:
            module.run_backup(module.plan())
        assert "named lock" in str(captured.value), str(captured.value)
        assert captured.value.hint, "a refusal is polite: it says what to do"
        assert module.health()["refusals"] == refusals + 1
        assert "lock" in {check.name for check in module.diagnostics()}
        assert module.run_backup(module.plan()).copied >= 1
    finally:
        chassis.shutdown()


def test_the_run_lock_is_a_named_lock_file(tmp_path: Path) -> None:
    """The lock is the primitive the chassis reports, not a private file in a corner."""
    chassis = backup_lock_chassis(tmp_path)
    try:
        module = chassis.registry.require("backup_engine").module
        module.run_backup(module.plan())
        assert chassis.paths.named_lock_files() == ("backup_engine.lock",)
        assert chassis.locks.names() == ()
    finally:
        chassis.shutdown()


def sweep_chassis(tmp_path: Path, **overrides: object) -> Chassis:
    """Return a chassis whose backup_engine copies a small tree on the chassis clock."""
    root = tmp_path / "project"
    source = root / "tiny"
    (source / "sub").mkdir(parents=True)
    (source / "a.txt").write_text("alpha\n", encoding="utf-8")
    (source / "sub" / "b.txt").write_text("beta\n", encoding="utf-8")
    settings: dict[str, object] = {
        "core.log.file_enabled": False,
        "core.ipc.enabled": False,
        "core.lifecycle.tick_interval": 0.02,
        "modules.backup_engine.source": str(source),
        "modules.backup_engine.schedule_interval": 0.05,
        "modules.backup_engine.auto_prune_days": 1,
    }
    settings.update(overrides)
    paths = AppPaths.discover(root=str(root), environ={})
    chassis = Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            install_signals=False,
            cli_overrides=settings,
        )
    )
    chassis.initialize()
    chassis.start(ipc=False)
    return chassis


def stamped_copies(dest: Path) -> list[Path]:
    """Return the children of *dest* whose own name is a backup stamp."""
    found: list[Path] = []
    for child in sorted(dest.iterdir()):
        try:
            datetime.strptime(child.name, STAMP_FORMAT)
        except ValueError:
            continue
        if child.is_dir():
            found.append(child)
    return found


def backdated(dest: Path, label: str, days: float) -> Path:
    """Create a stamped copy under *dest* as if it were made *days* ago."""
    moment = time.time() - days * 86_400.0
    copy = dest / default_stamp(moment)
    copy.mkdir(parents=True, exist_ok=True)
    os.utime(copy, (moment, moment))
    assert copy.name == label, "the helper names the copy by its stamp"
    return copy


def test_the_periodic_sweep_runs_through_the_named_lock(tmp_path: Path) -> None:
    """One module, both primitives: the clock runs the copy and the lock guards it."""
    chassis = sweep_chassis(tmp_path)
    try:
        module = chassis.registry.require("backup_engine").module
        dest = Path(module.settings().dest)
        assert {"schedule_interval", "auto_prune_days"} <= {
            field.key for field in module.config_schema().fields
        }
        task = chassis.scheduler.task("auto_sweep")
        assert task is not None, "a configured interval declares the task"
        assert task.module == "backup_engine", "the task belongs to the module that declared it"
        assert module.health()["sweep_scheduled"] is True
        assert chassis.locks.names() == (), "no lock is taken before the clock says it is due"
        assert chassis.run_forever(duration=0.25) == int(ExitCode.SUCCESS)
        assert module.health()["sweeps"] >= 1, "the clock ran the copy"
        copies = stamped_copies(dest)
        assert copies, "a swept copy landed in the destination"
        assert sorted(p.name for p in copies[0].rglob("*") if p.is_file()) == ["a.txt", "b.txt"]
        assert chassis.locks.names() == (), "a finished sweep releases the name it took"
        assert chassis.paths.named_lock_files() == ("backup_engine.lock",)
        assert chassis.supervise() == [], "a scheduled sweep loses no worker"
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["sweep"].severity is DiagnosticSeverity.INFO
        assert "auto_sweep" in rows["sweep"].message
    finally:
        chassis.shutdown()
    assert chassis.scheduler.task("auto_sweep") is None, "a stopped module leaves no task behind"


def test_a_sweep_steps_aside_for_a_copy_already_running(tmp_path: Path) -> None:
    """The lock decides whether the sweep may copy; a refusal is counted, never fatal."""
    chassis = sweep_chassis(tmp_path)
    try:
        module = chassis.registry.require("backup_engine").module
        dest = Path(module.settings().dest)
        dest.mkdir(parents=True, exist_ok=True)
        with chassis.lock("backup_engine", timeout=0.0):
            module._sweep_backup()
        assert stamped_copies(dest) == [], "a sweep that could not copy created no folder"
        assert module.health()["sweeps_refused"] == 1
        assert module.health()["sweeps"] == 0
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["lock"].severity is DiagnosticSeverity.WARNING
        assert rows["sweep"].severity is DiagnosticSeverity.INFO
        assert "stepped aside" in rows["sweep"].message
        module._sweep_backup()
        assert stamped_copies(dest), "the next sweep copies once the lock is free"
        assert module.health()["sweeps"] == 1
    finally:
        chassis.shutdown()


def test_auto_prune_ages_stamped_copies_out(tmp_path: Path) -> None:
    """'auto_prune_days' removes old stamped copies and never touches a hand-made folder."""
    chassis = sweep_chassis(tmp_path)
    try:
        module = chassis.registry.require("backup_engine").module
        dest = Path(module.settings().dest)
        old = backdated(dest, default_stamp(time.time() - 5 * 86_400.0), 5)
        hand = dest / "hand-made"
        hand.mkdir()
        assert module.health()["auto_prune_days"] == 1
        assert chassis.run_forever(duration=0.2) == int(ExitCode.SUCCESS)
        assert not old.exists(), "a copy past the retention window is gone"
        assert hand.is_dir(), "a folder the operator made is never treated as a backup"
        assert module.health()["pruned"] >= 1
        assert stamped_copies(dest), "the sweep still lands a fresh copy"
    finally:
        chassis.shutdown()


def test_zero_retention_keeps_every_copy(tmp_path: Path) -> None:
    """'auto_prune_days: 0' says keep every copy, however old it is."""
    chassis = sweep_chassis(tmp_path, **{"modules.backup_engine.auto_prune_days": 0})
    try:
        module = chassis.registry.require("backup_engine").module
        dest = Path(module.settings().dest)
        old = backdated(dest, default_stamp(time.time() - 400 * 86_400.0), 400)
        assert chassis.run_forever(duration=0.2) == int(ExitCode.SUCCESS)
        assert old.is_dir(), "no retention means no removal"
        assert module.health()["pruned"] == 0
    finally:
        chassis.shutdown()
