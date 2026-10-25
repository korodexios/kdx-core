"""Cross-platform single-instance lock: one daemon per chassis root."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from korodexios_core.exceptions import InstanceLockError
from korodexios_core.kernel.instance_lock import InstanceLock, LockHolder, pid_alive


def test_pid_alive_accepts_self_and_rejects_garbage() -> None:
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
    assert pid_alive(999_999_999) is False


def test_holder_shape_and_liveness() -> None:
    holder = LockHolder(pid=os.getpid(), label="kdx", host="127.0.0.1", port=8123)
    assert holder.is_alive is True
    assert holder.describe() == f"pid {os.getpid()} (kdx) at 127.0.0.1:8123"
    payload = holder.as_dict()
    assert payload["pid"] == os.getpid()
    assert payload["port"] == 8123
    assert LockHolder.from_mapping(payload).host == "127.0.0.1"
    assert LockHolder.from_mapping({}).pid == 0
    assert LockHolder(pid=0).describe() == "pid 0 (chassis)"
    assert LockHolder(pid=0).is_alive is False
    assert LockHolder(pid=0, started_at=1.0).uptime() > 0
    assert LockHolder(pid=0).uptime() == 0.0


def test_acquire_release_cycle(tmp_path: Path) -> None:
    path = tmp_path / "chassis.lock"
    lock = InstanceLock(path, metadata={"label": "kdx", "port": 8123})
    assert lock.held is False
    lock.acquire()
    assert lock.held is True
    assert path.exists()
    holder = lock.holder()
    assert holder is not None and holder.pid == os.getpid()
    assert holder is not None and holder.label == "kdx"
    assert lock.is_stale() is False
    assert lock.update({"label": "kdx", "port": 8124}) is lock
    assert "held by this process" in lock.describe()
    payload = lock.as_dict()
    assert payload["path"] == str(path)
    assert payload["held"] is True
    assert payload["holder"]["port"] == 8124
    lock.release()
    assert lock.held is False
    assert lock.holder() is None
    assert path.read_text(encoding="utf-8") == ""
    assert lock.update({"label": "kdx"}) is lock
    lock.release()


def test_second_holder_is_rejected_by_name(tmp_path: Path) -> None:
    path = tmp_path / "chassis.lock"
    first = InstanceLock(path, metadata={"label": "first"})
    first.acquire()
    second = InstanceLock(path, metadata={"label": "second"})
    with pytest.raises(InstanceLockError) as captured:
        second.acquire()
    message = captured.value.message
    assert str(path.parent) in message
    assert "first" in message
    assert captured.value.hint
    first.release()


def test_stale_metadata_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "chassis.lock"
    path.write_text(
        json.dumps({"pid": 999_999_999, "label": "ghost", "started_at": 1.0}),
        encoding="utf-8",
    )
    lock = InstanceLock(path)
    assert lock.is_stale() is True
    holder = lock.holder()
    assert holder is not None and holder.is_alive is False
    assert holder is not None and "ghost" in holder.describe()
    lock.acquire()
    assert lock.holder() is not None and lock.holder().pid == os.getpid()
    lock.release()


def test_dead_pid_stale_lock_is_taken_over_cleanly(tmp_path: Path) -> None:
    """A lock left behind by a killed daemon is adopted, not treated as a failure.

    The file still carries the old daemon JSON, but its PID is gone and the OS
    lock is free: acquire succeeds, the payload is rewritten to this process, and
    the lock is reported as held and fresh.
    """
    path = tmp_path / "chassis.lock"
    path.write_text(
        json.dumps({"pid": 999_999_999, "label": "killed", "started_at": 1.0}),
        encoding="utf-8",
    )
    lock = InstanceLock(path, metadata={"label": "fresh", "port": 8124})
    assert lock.is_stale() is True, "the pre-acquire scan still sees the dead owner"
    lock.acquire()
    assert lock.held is True
    holder = lock.holder()
    assert holder is not None and holder.pid == os.getpid()
    assert holder is not None and holder.label == "fresh"
    assert holder is not None and holder.is_alive is True
    assert lock.is_stale() is False, "the stale claim is gone once the payload is ours"
    lock.release()
    assert lock.held is False


def test_garbage_metadata_is_survivable(tmp_path: Path) -> None:
    path = tmp_path / "chassis.lock"
    path.write_text("not json at all", encoding="utf-8")
    lock = InstanceLock(path)
    assert lock.holder() is None
    assert lock.is_stale() is False
    lock.acquire()
    lock.release()


def test_context_manager_releases_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "chassis.lock"
    with InstanceLock(path, metadata={"label": "scoped"}) as lock:
        assert lock.held is True
        assert "chassis.lock" in str(lock)
    assert lock.held is False
    assert path.exists()


def test_lock_survives_a_second_process(tmp_path: Path) -> None:
    """A real second process cannot take the lock while the first holds it."""
    script = (
        "import sys;"
        "from korodexios_core.kernel.instance_lock import InstanceLock;"
        "lock = InstanceLock(sys.argv[1], metadata={'label': 'second'});"
        "lock.acquire();"
        "print('acquired')"
    )
    path = tmp_path / "chassis.lock"
    lock = InstanceLock(path, metadata={"label": "first"})
    lock.acquire()
    blocked = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert blocked.returncode != 0
    assert "InstanceLockError" in blocked.stderr
    assert "first" in blocked.stderr
    lock.release()
    free = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert free.returncode == 0, free.stderr
    assert free.stdout.strip() == "acquired"


def test_lock_creates_its_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state" / "chassis.lock"
    with InstanceLock(path) as lock:
        assert path.exists()
        assert lock.holder() is not None
