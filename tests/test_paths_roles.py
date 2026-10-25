"""Path roles: durable state lives apart from the runtime state directory."""

from __future__ import annotations

import pytest

from korodexios_core.exceptions import LockTimeoutError
from korodexios_core.paths import AppPaths, lock_name


def test_durable_dir_is_a_project_level_role(paths: AppPaths) -> None:
    assert paths.durable_dir == paths.root / "data"
    assert paths.durable_dir != paths.state_dir
    assert paths.state_dir == paths.root / "var"


def test_moving_the_state_dir_never_moves_the_durable_dir(paths: AppPaths) -> None:
    moved = paths.with_state_dir("state")
    assert moved.state_dir == paths.root / "state"
    assert moved.log_dir == paths.root / "state" / "log"
    assert moved.durable_dir == paths.durable_dir
    assert moved.locks_dir == paths.root / "state" / "locks"


def test_with_durable_dir_resolves_below_the_root(paths: AppPaths) -> None:
    relocated = paths.with_durable_dir("storage/keep")
    assert relocated.durable_dir == paths.root / "storage" / "keep"
    assert paths.with_durable_dir("") is paths


def test_ensure_directories_prepares_durable_and_locks(paths: AppPaths) -> None:
    created = paths.ensure_directories()
    assert paths.durable_dir.is_dir()
    assert paths.locks_dir.is_dir()
    assert paths.durable_dir in created
    assert paths.locks_dir in created
    assert len(created) == 5
    assert paths.ensure_directories() == created


def test_durable_module_dir_is_created_per_module(paths: AppPaths) -> None:
    first = paths.durable_module_dir("alpha")
    assert first == paths.durable_dir / "modules" / "alpha"
    assert first.is_dir()
    assert paths.durable_module_dir("beta").is_dir()


def test_as_dict_and_from_mapping_keep_every_role(paths: AppPaths) -> None:
    data = paths.as_dict()
    assert data["durable_dir"] == str(paths.durable_dir)
    assert data["locks_dir"] == str(paths.locks_dir)
    rebuilt = AppPaths.from_dict(data) if hasattr(AppPaths, "from_dict") else None
    del rebuilt
    from korodexios_core.paths import paths_from_mapping

    cloned = paths_from_mapping(data)
    assert cloned.durable_dir == paths.durable_dir
    assert cloned.state_dir == paths.state_dir
    assert cloned.data_dir == paths.data_dir


def test_lock_path_names_one_file_per_lock(paths: AppPaths) -> None:
    assert paths.lock_path("index") == paths.locks_dir / "index.lock"
    assert paths.lock_path("nightly sweep") == paths.locks_dir / "nightly sweep.lock"
    assert paths.named_lock_files() == ()
    assert paths.lock_file == paths.state_dir / "chassis.lock"


def test_app_paths_lock_acquires_and_releases(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = AppPaths.discover(root=str(root), environ={})
    paths.ensure_directories()
    handle = paths.lock("index")
    assert handle.held is True
    assert paths.named_lock_files() == ("index.lock",)
    with pytest.raises(LockTimeoutError):
        paths.lock("index")
    handle.release()
    assert handle.held is False
    assert paths.lock("index").held is True


@pytest.mark.parametrize(
    "candidate",
    ["", "   ", "../escape", "a/b", ".hidden", "..", "x" * 65, "back\\slash"],
)
def test_lock_name_rejects_unsafe_labels(candidate: str) -> None:
    with pytest.raises(ValueError):
        lock_name(candidate)


@pytest.mark.parametrize("candidate", ["index", "nightly sweep", "a.b-c_1", " Index "])
def test_lock_name_accepts_safe_labels(candidate: str) -> None:
    assert lock_name(candidate) == candidate.strip()
