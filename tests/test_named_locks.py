"""Named locks: isolated coordination points the chassis releases as one unit."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from korodexios_core.exceptions import LockTimeoutError
from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.kernel.locks import NamedLock, NamedLockRegistry


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
    assert 0.15 <= waited < 1.0, "the timeout was honoured, not skipped or doubled"
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
