"""backup_engine tests: planning, filters, budgets, dry runs, and the CLI surface."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.cli_contract import CliCommand
from korodexios_core.modules.backup_engine import (
    MODULE,
    BackupEngineModule,
    BackupError,
    BackupSettings,
    build_plan,
    execute_plan,
)
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
