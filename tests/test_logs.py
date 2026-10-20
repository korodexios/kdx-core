"""Log inspection tests: helpers plus the 'korodex logs' command."""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import ExitCode
from korodexios_core.logging.engine import (
    DEFAULT_TAIL_LINES,
    LogTailer,
    ReopeningRotatingFileHandler,
    rotate_log,
    tail_lines,
    truncate_log,
)


def make_log(tmp_path: Path, lines: int = 120) -> Path:
    target = tmp_path / "korodex.log"
    body = "".join(
        f"2026-10-20 12:00:0{i % 10} [INFO   ] korodex.probe: line {i}\n"
        for i in range(1, lines + 1)
    )
    target.write_text(body, encoding="utf-8")
    return target


def test_tail_lines_returns_the_tail_without_reading_the_head(tmp_path: Path) -> None:
    target = make_log(tmp_path)
    assert tail_lines(target, 5)[-1] == "2026-10-20 12:00:00 [INFO   ] korodex.probe: line 120"
    assert len(tail_lines(target, DEFAULT_TAIL_LINES)) == DEFAULT_TAIL_LINES
    assert tail_lines(target, 1) == tail_lines(target, 1)
    assert tail_lines(target, 0) == []
    assert tail_lines(tmp_path / "absent.log", 5) == []


def test_tail_lines_handles_an_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty.log"
    target.write_text("", encoding="utf-8")
    assert tail_lines(target, 10) == []


def test_truncate_log_empties_the_file_in_place(tmp_path: Path) -> None:
    target = make_log(tmp_path, lines=10)
    before = target.stat().st_size
    assert truncate_log(target) == before
    assert target.stat().st_size == 0
    assert tail_lines(target, 5) == []
    assert truncate_log(tmp_path / "absent.log") == 0


def test_rotate_log_archives_prunes_and_keeps_size_backups(tmp_path: Path) -> None:
    target = make_log(tmp_path, lines=10)
    before = target.stat().st_size
    archive, archived = rotate_log(target)
    assert archived == before
    assert archive.is_file()
    assert not target.exists()
    assert tail_lines(archive, 500), "the archive carries the old lines"
    # The rotating handler's size-based backups are a separate family.
    size_backup = tmp_path / "korodex.log.1"
    size_backup.write_text("size based backup\n", encoding="utf-8")
    for _ in range(3):
        target.write_text("fresh\n", encoding="utf-8")
        rotate_log(target, backup_count=1)
    timestamped = [
        name
        for name in (path.name for path in tmp_path.glob("korodex.log.*"))
        if re.match(r"^\d{8}-\d{6}(-\d+)?$", name[len("korodex.log.") :])
    ]
    assert len(timestamped) == 1, timestamped
    assert size_backup.is_file()
    with pytest.raises(FileNotFoundError):
        rotate_log(tmp_path / "absent.log")


def test_reopening_handler_notices_an_external_rotation(tmp_path: Path) -> None:
    target = tmp_path / "korodex.log"
    handler = ReopeningRotatingFileHandler(
        target, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=False
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("probe.rotation")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.info("before rotation")
        archive, _ = rotate_log(target)
        logger.info("after rotation")
        handler.flush()
        assert archive.read_text(encoding="utf-8").splitlines() == ["before rotation"]
        assert target.read_text(encoding="utf-8").splitlines() == ["after rotation"]
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_log_tailer_follows_appends_and_survives_truncation(tmp_path: Path) -> None:
    target = make_log(tmp_path, lines=3)
    tailer = LogTailer(target, poll_interval=0.05)
    tailer.seek_end()
    assert tailer.read_new() == []
    with target.open("a", encoding="utf-8") as handle:
        handle.write("first\npar")
    assert tailer.read_new() == ["first"]
    with target.open("a", encoding="utf-8") as handle:
        handle.write("tial\n")
    assert tailer.read_new() == ["partial"]
    target.write_text("after truncation\n", encoding="utf-8")
    assert tailer.read_new() == ["after truncation"]
    assert tailer.as_dict()["exists"] is True


def test_log_tailer_reports_a_missing_file_quietly(tmp_path: Path) -> None:
    tailer = LogTailer(tmp_path / "gone.log", poll_interval=0.05)
    tailer.seek_end()
    assert tailer.read_new() == []
    assert tailer.as_dict()["exists"] is False


def base_args(tmp_path: Path) -> list[str]:
    return ["--home", str(tmp_path), "--no-color"]


def test_logs_command_shows_the_recent_lines(capsys, tmp_path: Path) -> None:
    assert main([*base_args(tmp_path), "run", "--once"]) == int(ExitCode.SUCCESS)
    capsys.readouterr()
    assert main([*base_args(tmp_path), "logs", "-n", "5", "--raw"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    printed = [line for line in captured.out.splitlines() if "korodex.chassis" in line]
    assert printed, captured.out
    assert "chassis stopped" in captured.out


def test_logs_command_notes_a_missing_file(capsys, tmp_path: Path) -> None:
    # File logging is off, so the chassis never creates the file: the graceful path.
    assert main([*base_args(tmp_path), "--no-log-file", "logs"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "no log file found at" in captured.out
    assert "file logging is disabled" in captured.out


def test_logs_command_rejects_a_zero_line_count(capsys, tmp_path: Path) -> None:
    assert main([*base_args(tmp_path), "logs", "-n", "0"]) == int(ExitCode.ERROR)
    captured = capsys.readouterr()
    assert "--lines must be at least 1" in captured.out
    assert "korodex logs -n 200" in captured.out


def test_logs_clear_truncates_with_assent(capsys, tmp_path: Path) -> None:
    assert main([*base_args(tmp_path), "run", "--once"]) == int(ExitCode.SUCCESS)
    log_path = tmp_path / "var" / "log" / "korodex.log"
    assert log_path.is_file() and log_path.stat().st_size > 0
    before = log_path.stat().st_size
    assert main([*base_args(tmp_path), "--yes", "logs", "--clear"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "log file truncated" in captured.out
    after = log_path.stat().st_size
    # The truncation runs, then this very CLI session appends its own lifecycle lines.
    assert after < before // 2, (before, after)
    assert len(tail_lines(log_path, 500)) <= 3


def test_logs_rotate_archives_the_file(capsys, tmp_path: Path) -> None:
    assert main([*base_args(tmp_path), "run", "--once"]) == int(ExitCode.SUCCESS)
    log_path = tmp_path / "var" / "log" / "korodex.log"
    assert log_path.is_file()
    assert main([*base_args(tmp_path), "--yes", "logs", "--rotate"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "log file rotated" in captured.out
    assert sorted(log_path.parent.glob("korodex.log.*")), captured.out
    # The chassis reopens the path on its next write, so this very CLI session
    # lands in the fresh file rather than in the archive.
    assert "chassis stopped" in log_path.read_text(encoding="utf-8")


def test_logs_rotate_and_clear_conflict(capsys, tmp_path: Path) -> None:
    assert main([*base_args(tmp_path), "run", "--once"]) == int(ExitCode.SUCCESS)
    capsys.readouterr()
    assert main([*base_args(tmp_path), "logs", "--rotate", "--clear"]) == int(ExitCode.USAGE_ERROR)
    captured = capsys.readouterr()
    assert "mutually exclusive" in captured.out


def test_logs_follow_streams_appended_lines(capsys, tmp_path: Path) -> None:
    assert main([*base_args(tmp_path), "run", "--once"]) == int(ExitCode.SUCCESS)
    log_path = tmp_path / "var" / "log" / "korodex.log"
    assert log_path.is_file()

    def append() -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("2026-10-20 12:00:00 [INFO   ] korodex.probe: streamed marker\n")

    timer = threading.Timer(0.2, append)
    timer.start()
    try:
        code = main([*base_args(tmp_path), "logs", "-f", "--timeout", "0.8", "--raw"])
    finally:
        timer.cancel()
    assert code == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "streamed marker" in captured.out


def test_logs_command_is_offered_by_the_wizard(project_root) -> None:
    from korodexios_core.cli.commands import core_commands
    from korodexios_core.kernel.chassis import Chassis, ChassisOptions
    from korodexios_core.paths import AppPaths

    paths = AppPaths.discover(root=str(project_root), environ={})
    chassis = Chassis.create(
        ChassisOptions(home=paths.root, environ={}, cli_overrides={"core.log.file_enabled": False})
    )
    commands = {command.name: command for command in core_commands(chassis)}
    assert "logs" in commands
    assert commands["logs"].show_in_wizard is True
