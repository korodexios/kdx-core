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
    assert (info.major, info.minor, info.patch) == (0, 3, 2), __version__
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


def declared_excludes() -> tuple[list[str], list[str]]:
    """Return the wheel and sdist exclusion lists the build backend was told to honour."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = data["tool"]["hatch"]["build"]["targets"]
    wheel = targets["wheel"].get("exclude", [])
    sdist = targets["sdist"].get("exclude", [])
    assert isinstance(wheel, list) and isinstance(sdist, list)
    return [str(item) for item in wheel], [str(item) for item in sdist]


def test_packaging_declares_runtime_and_dev_exclusions() -> None:
    """Runtime state, scratch, and dev leftovers are excluded by name, not by .gitignore.

    A release built from a tarball inside a container has no VCS to ask, so what ships is
    decided by what the manifest says; a daemon's 'var/' archive or a half-written log file
    that rode into a wheel is a bug nobody can see from the outside.
    """
    wheel, sdist = declared_excludes()
    for marker in ("**/var", "/var", "**/*.log", "**/*.tmp", "**/__pycache__"):
        assert marker in wheel or marker in sdist, f"{marker} is not excluded anywhere"
    for junk in ("**/.pytest_cache", "**/.mypy_cache", "**/.ruff_cache"):
        assert junk in wheel, f"{junk} is not excluded from the wheel"
    for junk in ("/.coverage", "/htmlcov", "**/.coverage"):
        assert junk in sdist, f"{junk} is not excluded from the sdist"
    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = declared["tool"]["hatch"]["build"]["targets"]["sdist"].get("include", [])
    for wanted in ("README.md", "CHANGELOG.md", "llms.txt", "AGENTS.md"):
        assert wanted in [str(item) for item in includes], f"{wanted} must ship in the sdist"


def test_built_distributions_exclude_state_logs_and_dev_leftovers(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Build from a copy that has been run as a project: state, logs, and caches live there.

    The copy carries no VCS directory, so nothing is excluded on the authority of a
    '.gitignore' - only the manifest can keep a daemon's archives out of a publish.
    """
    work = tmp_path_factory.mktemp("artifact-audit")
    tree = work / "tree"
    out = work / "dist"
    shutil.copytree(
        REPO_ROOT,
        tree,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "venv",
            "var",
            "dist",
            "build",
            "htmlcov",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".coverage",
            "__pycache__",
            "*.pyc",
            "*.log",
            "*.tmp",
        ),
    )
    assert not (tree / ".git").exists(), "a copied tree has no VCS to inherit exclusions from"
    for stray in (
        "var/logs/chassis.log",
        "var/lib/backups/2026-10-20/payload.bin",
        "src/korodexios_core/kdx.log",
        "src/korodexios_core/stage.tmp",
        "src/korodexios_core/kernel/__pycache__/ipc.cpython-312.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".coverage",
        "htmlcov/index.html",
    ):
        path = tree / stray
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stray runtime state")
    pytest.importorskip("hatchling")
    completed = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-d", str(out)],
        cwd=str(tree),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheel = sorted(out.glob("*.whl"))[0]
    sdist = sorted(out.glob("*.tar.gz"))[0]
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = list(archive.namelist())
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = [name.split("/", 1)[1] for name in archive.getnames() if "/" in name]
    markers = ("/var/", "var/", ".log", ".tmp", "__pycache__", ".pyc", ".pytest_cache")
    for label, names in (("wheel", wheel_names), ("sdist", sdist_names)):
        for marker in markers:
            offenders = [name for name in names if marker in name]
            assert not offenders, f"{label} carries {marker}: {offenders[:3]}"
    assert not [name for name in sdist_names if name.startswith("htmlcov") or name == ".coverage"]
    assert any(name.endswith("py.typed") for name in wheel_names), "the type marker must ship"
