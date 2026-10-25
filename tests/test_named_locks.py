"""Named locks: isolated coordination points the chassis releases as one unit."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from korodexios_core.exceptions import LockTimeoutError
from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.kernel.locks import NamedLock, NamedLockRegistry
from korodexios_core.paths import AppPaths, is_lock_file_name


def make_registry(tmp_path: Path) -> NamedLockRegistry:
    """Return a registry rooted in a throwaway locks directory."""
    return NamedLockRegistry(tmp_path / "state" / "locks", poll_interval=0.01)


def test_acquire_holds_one_name_and_releases_it(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    handle = registry.acquire("index")
    assert handle.held is True
    assert handle.name == "index"
    assert handle.path == tmp_path / "state" / "locks" / "index.lock"
    assert handle.path.is_file()
    assert registry.held("index") is True
    assert registry.names() == ("index",)
    assert "index" in registry
    assert registry.release("index") is True
    assert handle.held is False
    assert registry.release("index") is False


def test_a_payload_write_failure_withdraws_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that cannot say who holds it is not one the next process can trust."""

    def unwritable(*_args: object) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("korodexios_core.kernel.locks.os.ftruncate", unwritable)
    registry = make_registry(tmp_path)
    with pytest.raises(OSError):
        registry.acquire("index")
    assert registry.names() == (), "a failed claim leaves no reservation"
    assert registry.held("index") is False
    assert (tmp_path / "state" / "locks" / "index.lock").is_file()

    monkeypatch.undo()
    recovered = registry.acquire("index")
    assert recovered.held is True
    assert registry.release("index") is True


def test_reacquiring_a_held_name_returns_the_same_handle(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    first = registry.acquire("index")
    assert registry.acquire("index") is first
    assert registry.names() == ("index",)


def test_two_names_never_queue_behind_each_other(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    index = registry.acquire("index")
    other = registry.acquire("nightly sweep", timeout=0.0)
    assert registry.names() == ("index", "nightly sweep")
    assert other.held is True
    assert index.held is True
    assert registry.describe() == "2 named lock(s) held: index, nightly sweep"
    assert registry.release_all() == 2
    assert registry.names() == ()


def test_a_separate_handle_cannot_take_a_held_name(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    held = registry.acquire("index")
    rival = NamedLock(held.path)
    with pytest.raises(LockTimeoutError) as captured:
        rival.acquire(timeout=0.0)
    assert captured.value.name == "index"
    assert "held by" in captured.value.message
    assert captured.value.render().count("lock: index") == 1


def test_a_timeout_waits_for_the_deadline_before_failing(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    held = registry.acquire("index")
    rival = NamedLock(held.path)
    started = time.monotonic()
    with pytest.raises(LockTimeoutError):
        rival.acquire(timeout=0.2)
    waited = time.monotonic() - started
    # A 200 ms timeout must be waited out, not skipped; the slack is the runner's,
    # where a descheduled thread can report a 200 ms wait as a longer one.
    assert 0.12 <= waited < 1.2, "the timeout was waited out, not skipped"
    assert held.held is True


def test_blocking_false_is_one_attempt(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    held = registry.acquire("index")
    rival = NamedLock(held.path)
    with pytest.raises(LockTimeoutError):
        rival.acquire(timeout=None, blocking=False)


def test_the_lock_releases_when_the_process_exits(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    handle = registry.acquire("index")
    handle.release()
    rival = NamedLock(handle.path)
    rival.acquire(timeout=0.0)
    assert rival.held is True
    rival.release()


def test_the_payload_names_the_live_owner(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    import os

    handle = registry.acquire("index", metadata={"label": "reindex"})
    holder = handle.holder()
    assert holder is not None
    assert holder.pid == os.getpid()
    assert handle.held_seconds >= 0.0
    assert "pid" in handle.describe()
    assert handle.as_dict()["stale"] is False
    assert handle.as_dict()["held"] is True


def test_a_dead_owner_leaves_a_stale_name(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.directory.mkdir(parents=True)
    stale_file = registry.directory / "abandoned.lock"
    stale_file.write_text('{"pid": 999999, "label": "abandoned", "name": "abandoned"}')
    assert registry.stale_names() == ("abandoned",)
    rows = registry.status()
    assert rows[0]["name"] == "abandoned"
    assert rows[0]["stale"] is True
    assert rows[0]["held"] is False
    payload = registry.as_dict()
    assert payload["on_disk"] == ["abandoned"]
    assert payload["stale"] == ["abandoned"]
    assert payload["directory"] == str(registry.directory)


def test_dead_owner_lock_is_taken_over_cleanly(tmp_path: Path) -> None:
    """A named lock whose recorded owner died is adopted without a manual delete.

    The descriptor is free (the owner is gone), so the OS lock succeeds; the file
    still names the dead PID. acquire must adopt the lock and rewrite the payload
    instead of raising LockTimeoutError.
    """
    import json
    import os

    registry = make_registry(tmp_path)
    registry.directory.mkdir(parents=True)
    stale_file = registry.directory / "abandoned.lock"
    stale_file.write_text(
        json.dumps({"pid": 999_999_999, "label": "abandoned", "name": "abandoned"}),
        encoding="utf-8",
    )
    assert registry.stale_names() == ("abandoned",), "the scan still reports the dead owner"
    handle = registry.acquire("abandoned", timeout=0.0, blocking=False)
    assert handle.held is True, "the stale lock is taken over, not refused"
    holder = handle.holder()
    assert holder is not None and holder.pid == os.getpid()
    assert holder is not None and holder.is_alive is True
    assert handle.is_stale() is False
    registry.release("abandoned")
    assert registry.held("abandoned") is False
    assert registry.stale_names() == (), "the payload no longer names a dead owner"


def test_a_live_owner_is_never_taken_over(tmp_path: Path) -> None:
    """A lock whose owner is still alive is refused, not silently adopted."""
    registry = make_registry(tmp_path)
    held = registry.acquire("index")
    rival = NamedLock(held.path)
    with pytest.raises(LockTimeoutError):
        rival.acquire(timeout=0.0)
    assert held.held is True, "the live holder keeps its lock"
    registry.release("index")


def test_hold_is_a_context_manager_and_refuses_nesting(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    with registry.hold("index") as handle:
        assert handle.held is True
        assert registry.held("index") is True
    assert registry.held("index") is False
    with registry.hold("index", timeout=0.5) as held:
        assert held.held is True
        # Nesting the same name is a design smell, refused at the call site.
        with pytest.raises(LockTimeoutError), registry.hold("index"):
            pass


def test_an_unsafe_name_is_rejected_at_the_call_site(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    for candidate in ["../escape", "a/b", ".hidden", ""]:
        with pytest.raises(ValueError):
            registry.acquire(candidate)


def test_the_chassis_sheds_every_named_lock_on_shutdown(project_root: Path) -> None:
    write_root(project_root)
    chassis = Chassis.create(
        ChassisOptions(home=project_root, environ={}, install_signals=False)
    )
    chassis.start(ipc=False)
    try:
        assert chassis.locks is not None
        handle = chassis.acquire_lock("index", timeout=0.5)
        assert handle.held is True
        assert chassis.named_lock("index") is handle
        assert chassis.context.acquire_lock("other", timeout=0.5).held is True
        with chassis.lock("third", timeout=0.5) as held:
            assert held.held is True
        assert chassis.named_lock("third") is None
        assert chassis.context.paths.lock_path("index") == handle.path
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()
    assert chassis.locks is not None
    assert chassis.locks.names() == ()


def test_a_second_process_isolates_only_its_own_name(project_root: Path) -> None:
    write_root(project_root)
    chassis = Chassis.create(
        ChassisOptions(home=project_root, environ={}, install_signals=False)
    )
    chassis.start(ipc=False)
    try:
        index = chassis.acquire_lock("index")
        rival = NamedLock(chassis.paths.lock_path("index"))
        with pytest.raises(LockTimeoutError):
            rival.acquire(timeout=0.0)
        free = NamedLock(chassis.paths.lock_path("other"))
        free.acquire(timeout=0.0)
        assert free.held is True
        free.release()
        assert index.held is True
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def write_root(root: Path) -> None:
    """Write a throwaway root with the control plane switched off."""
    (root / "kdx.yml").write_text(
        root.joinpath("kdx.yml").read_text(encoding="utf-8").replace(
            "ipc:", "ipc:", 1
        ),
        encoding="utf-8",
    )


def test_editor_artefacts_are_not_locks(tmp_path: Path) -> None:
    """A swap file in the locks directory is not a lock the chassis should report."""
    registry = make_registry(tmp_path)
    registry.directory.mkdir(parents=True)
    payload = '{"pid": 999999, "label": "gone", "name": "dead"}'
    for name in (".backup.lock", "..lock", "notes:bad.lock", "index.lock", "notes.txt"):
        (registry.directory / name).write_text("", encoding="utf-8")
    (registry.directory / ".dead.lock").write_text(payload, encoding="utf-8")
    (registry.directory / "dead.lock").write_text(payload, encoding="utf-8")
    assert [item.name for item in registry._scan_files()] == [
        "dead.lock",
        "index.lock",
    ]
    assert registry.as_dict()["on_disk"] == ["dead", "index"]
    assert registry.stale_names() == ("dead",), "a dot file is never a stale owner"


def test_named_lock_files_ignores_files_a_lock_cannot_name(tmp_path: Path) -> None:
    """'kdx doctor' reads this directory: an editor's leftovers never look like a lock."""
    root = tmp_path / "project"
    root.mkdir(parents=True)
    paths = AppPaths.discover(root=str(root), environ={})
    paths.locks_dir.mkdir(parents=True, exist_ok=True)
    for name in (".backup.lock", "backup.lock", "index.lock", "bad:lock.lock", "notes.txt"):
        (paths.locks_dir / name).write_text("", encoding="utf-8")
    assert paths.named_lock_files() == ("backup.lock", "index.lock")
    assert is_lock_file_name("backup.lock") is True
    assert is_lock_file_name(".backup.lock") is False
    assert is_lock_file_name("bad:lock.lock") is False
    assert is_lock_file_name("notes.txt") is False
    assert is_lock_file_name("notes.lock.lock") is True
