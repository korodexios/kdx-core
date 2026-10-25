"""A relative --config or --env-file is anchored to the chassis root, not to the CWD."""

from __future__ import annotations

from pathlib import Path

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import ExitCode
from korodexios_core.exceptions import PathDiscoveryError
from korodexios_core.paths import AppPaths


def project(tmp_path: Path) -> Path:
    """Return a project root two levels below the temporary directory."""
    root = tmp_path / "outer" / "project"
    root.mkdir(parents=True)
    (root / "kdx.yml").write_text("core: {}\n", encoding="utf-8")
    (root / ".env.local").write_text("KDX_FROM_TEST=1\n", encoding="utf-8")
    (root / "alt.yml").write_text("core: {}\n", encoding="utf-8")
    return root


def test_relative_config_is_anchored_to_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative config path says where the file sits under the root, not in the CWD."""
    root = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    paths = AppPaths.discover(root=str(root), environ={}, config_file="kdx.yml")
    assert paths.config_file == root / "kdx.yml"


def test_dotted_relative_config_is_anchored_once(tmp_path: Path) -> None:
    """A path written with './' and '..' is resolved against the root exactly once."""
    root = project(tmp_path)
    paths = AppPaths.discover(root=str(root), environ={}, config_file="./alt.yml")
    assert paths.config_file == root / "alt.yml"
    nested = AppPaths.discover(root=str(root), environ={}, config_file="sub/../kdx.yml")
    assert nested.config_file == root / "kdx.yml"


def test_relative_env_file_is_anchored_to_the_root(tmp_path: Path) -> None:
    """A dotenv override follows the same anchoring rule as the config override."""
    root = project(tmp_path)
    paths = AppPaths.discover(root=str(root), environ={}, env_file=".env.local")
    assert paths.env_file == root / ".env.local"


def test_environment_config_path_is_anchored_to_the_root(tmp_path: Path) -> None:
    """KDX_CONFIG may hold a relative path: the root still decides where it is."""
    root = project(tmp_path)
    paths = AppPaths.discover(root=str(root), environ={"KDX_CONFIG": "alt.yml"})
    assert paths.config_file == root / "alt.yml"


def test_absolute_paths_are_used_exactly_as_given(tmp_path: Path) -> None:
    """An absolute override is a fact about the filesystem, not about the root."""
    root = project(tmp_path)
    outside = tmp_path / "outer" / "alt.yml"
    outside.write_text("core: {}\n", encoding="utf-8")
    paths = AppPaths.discover(root=str(root), environ={}, config_file=str(outside))
    assert paths.config_file == outside.resolve()


def test_a_missing_relative_config_is_reported_against_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error names the path the chassis actually looked for."""
    root = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PathDiscoveryError) as captured:
        AppPaths.discover(root=str(root), environ={}, config_file="nope.yml")
    assert str(root / "nope.yml") in str(captured.value)
    assert str(tmp_path / "nope.yml") not in str(captured.value)


def test_the_cli_passes_a_relative_config_through(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running kdx from another directory still reads --config kdx.yml under --home."""
    root = project(tmp_path)
    monkeypatch.chdir(tmp_path / "outer")
    args = [
        "--home",
        str(root),
        "--config",
        "kdx.yml",
        "--no-log-file",
        "--no-color",
        "config",
        "check",
    ]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert str(root / "kdx.yml") in captured.out
