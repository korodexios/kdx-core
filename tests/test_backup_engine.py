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
from korodexios_core.modules.backup_engine.module import PRUNE_TASK, PRUNED_EVENT
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
    # write_bytes keeps the fixtures byte-exact: write_text would translate the
    # trailing newline to os.linesep on Windows and the size assertions drift.
    (source / "alpha.txt").write_bytes(b"alpha\n")
    (source / "nested" / "beta.txt").write_bytes(b"beta contents\n")
    (source / "notes.log").write_bytes(b"noise\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "cached.pyc").write_bytes(b"junk\n")
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
    names = sorted(path.relative_to(source_tree).as_posix() for path in plan.files)
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


def test_same_second_backups_get_incremented_targets(source_tree: Path, tmp_path: Path) -> None:
    """Three runs inside one second plan three distinct, isolated destination folders.

    ``build_plan`` only inspects the source; the folders appear as each run copies.
    So the first folder is created the way a finished run would leave it, and the next
    two plans of the same stamp must step aside into suffixed siblings.
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    label = default_stamp()
    (dest / label).mkdir(),  # the copy the first run already made
    first = build_plan(settings_for(source_tree, dest, stamp=label))
    (dest / f"{label}-1").mkdir(),  # a second run landed before the third planned
    second = build_plan(settings_for(source_tree, dest, stamp=label))
    third = build_plan(settings_for(source_tree, dest, stamp=label))
    assert first.target.name == f"{label}-1"
    assert second.target.name == f"{label}-2"
    assert third.target.name == f"{label}-2", "planning never writes, so it keeps its answer"
    # The first plan took the suffix the free name implied; the next two agree on
    # the next free suffix, because planning reads the tree instead of mutating it.
    assert first.target.name != second.target.name


def test_incremented_targets_isolated_when_first_run_executed(
    source_tree: Path, tmp_path: Path
) -> None:
    """A run that has already copied keeps the next same-second run in its own folder."""
    dest = tmp_path / "dest"
    dest.mkdir()
    label = default_stamp()
    first = build_plan(settings_for(source_tree, dest, stamp=label))
    execute_plan(first, dry_run=False)
    assert first.target.is_dir()
    second = build_plan(settings_for(source_tree, dest, stamp=label))
    assert second.target.name == f"{label}-1"
    execute_plan(second, dry_run=False)
    assert sorted(p.name for p in dest.iterdir()) == [label, f"{label}-1"]


def test_readonly_destination_is_refused_or_skipped_on_windows(tmp_path: Path) -> None:
    """A destination the OS will not let us write into is reported, not clobbered.

    On POSIX a ``0o500`` directory denies ``os.access`` and the copy raises
    ``PermissionError``. On Windows the filesystem ignores POSIX mode bits (the
    read-only flag is what NTFS maps them onto), so the refusal cannot be
    simulated the same way: the test skips there rather than assert a refusal
    the platform never honours.
    """
    if os.name == "nt":
        pytest.skip("POSIX directory permissions are not enforced on Windows")
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.chmod(dest, 0o500)
    try:
        if os.access(dest, os.W_OK):
            # A runner that ignores mode bits cannot express the refusal: skip the
            # assertion rather than fail it, since the platform never honours 0o500.
            pytest.skip("the platform does not deny write access to a 0o500 directory")
        plan = build_plan(
            BackupSettings(source=source, dest=dest, include=("*",), exclude=()),
            stamp="run-ro",
        )
        with pytest.raises(PermissionError):
            execute_plan(plan, dry_run=False)
        assert not (dest / "run-ro").exists(), "a refused copy leaves no partial folder"
    finally:
        os.chmod(dest, 0o755)


def test_explicit_stamp_keeps_its_name_when_free(tmp_path: Path) -> None:
    """A hand-picked stamp that is still free is used verbatim, with no suffix."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    plan = build_plan(
        BackupSettings(source=source, dest=tmp_path / "dest"), stamp="operator-run"
    )
    assert plan.target.name == "operator-run"


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
    assert sorted(item.relative_to(source).as_posix() for item in plan.files) == ["src/a.txt"]
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


def test_a_retention_window_declares_its_own_task(tmp_path: Path) -> None:
    """Retention is maintenance work of its own, not a side effect of a copy landing."""
    chassis = sweep_chassis(
        tmp_path,
        **{
            "modules.backup_engine.schedule_interval": 0.0,
            "modules.backup_engine.prune_interval": 0.05,
        },
    )
    try:
        module = chassis.registry.require("backup_engine").module
        assert chassis.scheduler.task("auto_sweep") is None, "no copy is scheduled here"
        task = chassis.scheduler.task(PRUNE_TASK)
        assert task is not None, "a retention interval with a window declares its own task"
        assert task.module == "backup_engine", "the task belongs to the module that declared it"
        health = module.health()
        assert health["prune_interval_seconds"] == 0.05
        assert health["prune_scheduled"] is True
        assert health["prune_sweeps"] == 0, "the clock has not run it yet"
        assert "prune_interval" in {field.key for field in module.config_schema().fields}
        window = next(
            field for field in module.config_schema().fields if field.key == "prune_interval"
        )
        assert window.default == 0.0, "an independent sweep is opt-in, like every other interval"
        assert window.description
    finally:
        chassis.shutdown()
    assert chassis.scheduler.task(PRUNE_TASK) is None, "a stopped module leaves no task behind"


def test_the_retention_sweep_ages_archives_out_without_a_copy(tmp_path: Path) -> None:
    """The sweep thins the destination on its own beat and reports the space it took back."""
    chassis = sweep_chassis(
        tmp_path,
        **{
            "modules.backup_engine.schedule_interval": 0.0,
            "modules.backup_engine.prune_interval": 0.05,
        },
    )
    try:
        module = chassis.registry.require("backup_engine").module
        dest = Path(module.settings().dest)
        old = backdated(dest, default_stamp(time.time() - 3 * 86_400.0), 3)
        (old / "payload.bin").write_bytes(b"0123456789abcde")
        hand = dest / "hand-made"
        hand.mkdir()
        assert chassis.run_forever(duration=0.2) == int(ExitCode.SUCCESS)
        assert not old.exists(), "the clock aged the archive out with no copy in sight"
        assert hand.is_dir(), "a folder the operator made is never treated as a backup"
        assert stamped_copies(dest) == [], "retention copied nothing"
        health = module.health()
        assert health["prune_sweeps"] >= 1, "the clock ran the sweep"
        # A slow runner may fit only one beat into the window, and that beat is the
        # one that aged the archive out: asserting the last beat removed nothing
        # would measure how busy the runner is, not what retention does. The total
        # below is the runner-independent truth: exactly one archive was taken.
        if health["prune_sweeps"] >= 2:
            assert health["prune_last_removed"] == 0, "a second beat finds nothing left to age out"
        assert health["pruned"] == 1, "the archive the first beat took is counted"
        assert health["prune_reclaimed_bytes"] == 15
        assert health["prune_reclaimed"] == "15 bytes"
        assert health["prune_last_run_seconds_ago"] is not None
        pruned = [event for event in chassis.bus.history() if event.name == PRUNED_EVENT]
        assert pruned, "the sweep published what it removed"
        assert pruned[-1].payload["reclaimed_bytes"] == 15
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["retention"].severity is DiagnosticSeverity.INFO
        assert "auto_prune" in rows["retention"].message
        assert chassis.locks.names() == (), "a finished sweep releases the name it took"
    finally:
        chassis.shutdown()


def test_the_retention_sweep_steps_aside_for_a_copy_holding_the_lock(tmp_path: Path) -> None:
    """Pruning shares the copy's named lock, so an archive is never deleted beside a copy."""
    chassis = sweep_chassis(
        tmp_path,
        **{
            "modules.backup_engine.schedule_interval": 0.0,
            "modules.backup_engine.prune_interval": 0.05,
        },
    )
    try:
        module = chassis.registry.require("backup_engine").module
        dest = Path(module.settings().dest)
        old = backdated(dest, default_stamp(time.time() - 3 * 86_400.0), 3)
        (old / "payload.bin").write_bytes(b"0123456789abcde")
        with chassis.lock("backup_engine", timeout=0.0):
            module._prune_sweep()
        assert old.is_dir(), "a prune never runs beside a copy that is still landing"
        health = module.health()
        assert health["prune_sweeps"] == 0
        assert health["prune_sweeps_refused"] == 1, "a refusal is counted, never fatal"
        module._prune_sweep()
        assert not old.exists(), "the next sweep ages the archive once the name is free"
        assert module.health()["prune_sweeps"] == 1
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["retention"].severity is DiagnosticSeverity.INFO
    finally:
        chassis.shutdown()


def test_a_retention_interval_without_a_window_is_reported(tmp_path: Path) -> None:
    """An interval with no retention window has nothing to age out, and says so."""
    chassis = sweep_chassis(
        tmp_path,
        **{
            "modules.backup_engine.schedule_interval": 0.0,
            "modules.backup_engine.auto_prune_days": 0,
            "modules.backup_engine.prune_interval": 60.0,
        },
    )
    try:
        module = chassis.registry.require("backup_engine").module
        assert chassis.scheduler.task(PRUNE_TASK) is None, "a sweep with no window stays home"
        assert module.health()["prune_interval_seconds"] == 60.0
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["retention"].severity is DiagnosticSeverity.WARNING
        assert "no window to age out" in rows["retention"].message
    finally:
        chassis.shutdown()


def test_retention_left_at_zero_is_left_to_the_copy(tmp_path: Path) -> None:
    """At the default 'prune_interval: 0' a copy still prunes inline, exactly as before."""
    chassis = sweep_chassis(tmp_path, **{"modules.backup_engine.prune_interval": 0.0})
    try:
        module = chassis.registry.require("backup_engine").module
        dest = Path(module.settings().dest)
        old = backdated(dest, default_stamp(time.time() - 3 * 86_400.0), 3)
        (old / "payload.bin").write_bytes(b"0123456789abcde")
        assert chassis.scheduler.task(PRUNE_TASK) is None, "no extra task is declared"
        assert module.health()["prune_scheduled"] is False
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["retention"].severity is DiagnosticSeverity.INFO
        assert "only as a copy lands" in rows["retention"].message
        assert chassis.run_forever(duration=0.2) == int(ExitCode.SUCCESS)
        assert not old.exists(), "the copy that landed pruned the expired archive inline"
        assert stamped_copies(dest), "the sweep still lands a fresh copy"
    finally:
        chassis.shutdown()


def test_a_copy_prunes_inline_and_reports_the_space_it_took_back(tmp_path: Path) -> None:
    """Space accounting covers an inline prune too: 'kdx doctor' can check it either way."""
    chassis = sweep_chassis(tmp_path, **{"modules.backup_engine.prune_interval": 0.0})
    try:
        module = chassis.registry.require("backup_engine").module
        dest = Path(module.settings().dest)
        old = backdated(dest, default_stamp(time.time() - 4 * 86_400.0), 4)
        (old / "payload.bin").write_bytes(b"0123456789abcde")
        assert chassis.run_forever(duration=0.2) == int(ExitCode.SUCCESS)
        health = module.health()
        assert health["prune_sweeps"] == 0, "an inline prune is not a scheduled sweep"
        assert health["pruned"] >= 1, "the removed copy is still counted"
        assert health["prune_reclaimed_bytes"] == 15
        assert health["prune_reclaimed"] == "15 bytes"
    finally:
        chassis.shutdown()


def test_an_initialized_module_reports_a_task_it_never_declared(tmp_path: Path) -> None:
    """'kdx doctor' initializes the modules it inspects and never starts them.

    The clock is there and the task is not, so the honest row says the sweep is waiting
    for the chassis to start - not that the chassis has no clock, which is a warning an
    operator cannot act on while the module is merely inspected.
    """
    root = tmp_path / "project"
    root.mkdir()
    paths = AppPaths.discover(root=str(root), environ={})
    chassis = Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            install_signals=False,
            cli_overrides={
                "core.log.file_enabled": False,
                "core.ipc.enabled": False,
                "modules.backup_engine.auto_prune_days": 1,
                "modules.backup_engine.prune_interval": 60.0,
            },
        )
    )
    chassis.initialize()
    try:
        module = chassis.registry.require("backup_engine").module
        assert chassis.scheduler is not None and chassis.scheduler.running is False
        assert module.health()["prune_scheduled"] is False
        assert module.health()["prune_interval_seconds"] == 60.0
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["retention"].severity is DiagnosticSeverity.INFO
        assert "waits for the chassis to start" in rows["retention"].message
    finally:
        chassis.shutdown()


def test_a_withdrawn_retention_task_is_reported_as_gone(tmp_path: Path) -> None:
    """A task withdrawn behind the module's back is a warning, not a quiet report."""
    chassis = sweep_chassis(
        tmp_path,
        **{
            "modules.backup_engine.schedule_interval": 0.0,
            "modules.backup_engine.prune_interval": 0.05,
        },
    )
    try:
        module = chassis.registry.require("backup_engine").module
        assert chassis.scheduler.task(PRUNE_TASK) is not None
        assert module.health()["prune_scheduled"] is True
        module._stop_prune()
        assert module.health()["prune_scheduled"] is False
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["retention"].severity is DiagnosticSeverity.WARNING
        assert "declared no task" in rows["retention"].message
        module._start_prune()
        assert chassis.scheduler.task(PRUNE_TASK) is not None, "the module re-declares it"
        chassis.scheduler.unschedule(PRUNE_TASK)
        rows = {check.name: check for check in module.diagnostics()}
        assert rows["retention"].severity is DiagnosticSeverity.WARNING
        assert "is gone" in rows["retention"].message
        assert module.health()["prune_scheduled"] is True, "the module still believes it scheduled"
    finally:
        chassis.shutdown()
