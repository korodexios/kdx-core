"""The 'kdx backup retention' report: policy, standing copies, and the sweep record."""

from __future__ import annotations

import json
import time
from pathlib import Path

from korodexios_core.cli.entry import main
from korodexios_core.constants import ExitCode
from korodexios_core.modules.backup_engine import BackupEngineModule
from korodexios_core.modules.backup_engine.engine import default_stamp
from tests.test_backup_engine import (
    RecordingSurface,
    backdated,
    base_args,
    sweep_chassis,
)


def quiet_chassis(tmp_path: Path, **overrides: object) -> BackupEngineModule:
    """Return a backup module with no clock work scheduled, so the report is the only actor."""
    settings: dict[str, object] = {
        "modules.backup_engine.schedule_interval": 0.0,
        "modules.backup_engine.prune_interval": 0.0,
    }
    settings.update(overrides)
    chassis = sweep_chassis(tmp_path, **settings)
    return chassis.registry.require("backup_engine").module


def test_report_counts_standing_copies_and_the_window(tmp_path: Path) -> None:
    """The report reads the destination by the same rule a sweep deletes by."""
    module = quiet_chassis(tmp_path)
    dest = Path(module.settings().dest)
    dest.mkdir(parents=True, exist_ok=True)
    fresh = dest / default_stamp()
    fresh.mkdir()
    (fresh / "keep.bin").write_bytes(b"x" * 120)
    old = backdated(dest, default_stamp(time.time() - 40 * 86_400.0), 40)
    (old / "gone.bin").write_bytes(b"y" * 240)
    (dest / "operator-folder").mkdir()
    report = module.retention_report()
    assert report.window_days == 1, "the sweep_chassis window is one day"
    assert report.copies_standing == 2, "only stamped folders count; the operator's folder does not"
    assert report.expired_now == 1, "the 40-day copy stands past the one-day window"
    assert report.copies_bytes == 360
    assert report.oldest_stamp == old.name
    assert report.newest_stamp == fresh.name
    assert "360" in report.as_dict()["copies_human"]


def test_zero_window_reports_no_expiry(tmp_path: Path) -> None:
    """No window means nothing is ever past it: the report says so, it does not guess."""
    module = quiet_chassis(tmp_path, **{"modules.backup_engine.auto_prune_days": 0})
    dest = Path(module.settings().dest)
    backdated(dest, default_stamp(time.time() - 400 * 86_400.0), 400)
    report = module.retention_report()
    assert report.window_days == 0
    assert report.copies_standing == 1
    assert report.expired_now == 0, "no window means nothing is expired"


def test_missing_destination_reports_no_copies_without_failing(tmp_path: Path) -> None:
    module = quiet_chassis(tmp_path)
    report = module.retention_report()
    assert report.copies_standing == 0
    assert report.oldest_stamp == "" and report.newest_stamp == ""


def test_retention_subcommand_is_declared() -> None:
    surface = RecordingSurface()
    BackupEngineModule().register_cli(surface)
    backup = surface.added[0]
    names = {command.name for command in backup.subcommands}
    assert "retention" in names, "the group carries the retention report"


def test_retention_route_is_declared(tmp_path: Path) -> None:
    module = quiet_chassis(tmp_path)
    routes = {route.path for route in module.ipc_routes()}
    assert "/api/v1/backup/retention" in routes
    payload = module.retention_report().as_dict()
    assert payload["window_days"] == 1
    assert payload["dest"] == str(module.settings().dest)


def test_cli_retention_text_and_json(capsys, project_root) -> None:
    dest = Path(project_root) / "backups"
    # Name the destination in the project's own config: the report reads the
    # configured dest, exactly as a sweep would.
    (Path(project_root) / "kdx.yml").write_text(
        f"modules:\n  backup_engine:\n    dest: {dest}\n", encoding="utf-8"
    )
    copy = dest / default_stamp()
    copy.mkdir(parents=True)
    (copy / "a.bin").write_bytes(b"z" * 30)
    args = [*base_args(project_root), "backup", "retention"]
    assert main(args) == int(ExitCode.SUCCESS)
    out = capsys.readouterr().out
    assert "Backup retention" in out
    assert "copies standing" in out

    payload_args = [*base_args(project_root), "backup", "retention", "--json"]
    assert main(payload_args) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["copies_standing"] == 1
    assert payload["copies_bytes"] == 30
    assert payload["window_days"] == 0, "the default window is off"
