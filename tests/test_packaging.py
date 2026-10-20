"""Distribution tests: one version source, complete package data."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from korodexios_core.kernel.discovery import version_satisfies
from korodexios_core.version import (
    CORE_API_VERSION,
    __version__,
    format_version,
    parse_version,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "korodexios_core"
REQUIRED_WHEEL_ENTRIES = (
    "korodexios_core/py.typed",
    "korodexios_core/kernel/ipc.py",
    "korodexios_core/cli/init_project.py",
    "korodexios_core/modules/heartbeat/kdx_module.toml",
    "korodexios_core/modules/system_info/kdx_module.toml",
    "korodexios_core/modules/backup_engine/kdx_module.toml",
)
REQUIRED_SDIST_ENTRIES = (
    "AGENTS.md",
    "llms.txt",
    "docs/AGENTS_GUIDE.md",
    "docs/ARCHITECTURE.md",
    "docs/CLI_REFERENCE.md",
    "docs/TUTORIAL.md",
    "docs/ROADMAP.md",
    ".agents/skills/verification.md",
    "README.md",
    "LICENSE",
    "kdx.yml",
    "pyproject.toml",
    "Makefile",
    "src/korodexios_core/py.typed",
    "tests/test_ipc.py",
    "tests/test_init_project.py",
    "tests/test_logs.py",
)


def read_pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_version_is_declared_once() -> None:
    project = read_pyproject()["project"]
    assert project["dynamic"] == ["version"]
    assert "version" not in project
    assert __version__ != ""


def test_hatch_version_pattern_matches_the_declared_version() -> None:
    """The build reads the version by regex; the pattern must match the real line."""
    hatch = read_pyproject()["tool"]["hatch"]["version"]
    assert Path(str(hatch["path"])).resolve() == (PACKAGE_DIR / "version.py").resolve()
    pattern = str(hatch["pattern"])
    source = (PACKAGE_DIR / "version.py").read_text(encoding="utf-8")
    match = re.search(pattern, source, re.MULTILINE)
    assert match is not None, "hatch version pattern no longer matches version.py"
    assert match.group("version") == __version__


def test_version_string_is_well_formed_and_accepted_by_modules() -> None:
    info = parse_version(__version__)
    assert (info.major, info.minor, info.patch) == (0, 1, 0), __version__
    expected = f"korodexios-core {__version__} (core api v{CORE_API_VERSION}, python chassis)"
    assert format_version() == expected
    assert version_satisfies(__version__, ">=0.1,<1.0") is True


def test_py_typed_marker_ships_with_the_package() -> None:
    assert (PACKAGE_DIR / "py.typed").is_file()


def test_console_scripts_are_declared() -> None:
    scripts = read_pyproject()["project"]["scripts"]
    expected = "korodexios_core.cli.entry:main"
    assert scripts["kdx"] == expected
    assert scripts["korodexios"] == expected
    assert len(scripts) == 2, "every console script must reach the same entry point"


def build_distribution(tmp_path: Path) -> tuple[Path, Path]:
    """Build wheel and sdist with the installed backend, skipping when unavailable."""
    pytest.importorskip("hatchling")
    module_command = [
        sys.executable,
        "-m",
        "hatchling",
        "build",
        "-d",
        str(tmp_path),
    ]
    completed = subprocess.run(
        module_command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0 and shutil.which("uv"):
        uv_command = ["uv", "build", "--out-dir", str(tmp_path)]
        completed = subprocess.run(
            uv_command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = sorted(tmp_path.glob("*.whl"))
    sdists = sorted(tmp_path.glob("*.tar.gz"))
    assert wheels and sdists, f"build produced: {sorted(p.name for p in tmp_path.iterdir())}"
    return wheels[0], sdists[0]


def test_built_wheel_carries_code_markers_and_manifests(tmp_path: Path) -> None:
    wheel, _ = build_distribution(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in archive.namelist() if name.endswith("METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    for entry in REQUIRED_WHEEL_ENTRIES:
        assert entry in names, f"wheel is missing {entry}"
    assert f"Version: {__version__}" in metadata
    assert "Requires-Dist: pyyaml" in metadata
    assert "Requires-Dist: rich" in metadata
    assert not [name for name in names if name.startswith("tests/")], "tests leaked into the wheel"


def test_built_sdist_carries_docs_skills_and_tests(tmp_path: Path) -> None:
    _, sdist = build_distribution(tmp_path)
    with tarfile.open(sdist, "r:gz") as archive:
        inner = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    for entry in REQUIRED_SDIST_ENTRIES:
        assert entry in inner, f"sdist is missing {entry}"
