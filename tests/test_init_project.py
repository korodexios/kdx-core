"""Project templating tests: 'korodex init' writes a runnable repository."""

from __future__ import annotations

import py_compile
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from korodexios_core.cli.commands import core_commands
from korodexios_core.cli.entry import main
from korodexios_core.cli.init_project import (
    DEFAULT_AUTHOR,
    ECOSYSTEM_LABELS,
    EXAMPLE_MODULES,
    ProjectError,
    build_files,
    ecosystem_key,
    init_project,
    project_from_inputs,
    validate_project_name,
)
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.paths import AppPaths


def make_project(name: str = "demo-app", **kwargs: object) -> object:
    return project_from_inputs(name, **kwargs)  # type: ignore[arg-type]


def test_project_from_inputs_normalises_the_identity() -> None:
    project = project_from_inputs("Demo-App", ecosystem="fullstack")
    assert project.package == "demo_app"
    assert project.distribution == "demo-app"
    assert project.title == "Demo App"
    assert project.script == "demo-app"
    assert project.author == DEFAULT_AUTHOR
    assert project.fullstack is True
    assert project.core_version


def test_project_names_are_validated_politely() -> None:
    assert validate_project_name("my_service").ok
    assert str(validate_project_name("My-Service").value) == "my_service"
    for bad in ("", "9lives", "korodex", "korodexios_core", "has space"):
        assert not validate_project_name(bad).ok, bad
    with pytest.raises(ProjectError, match="project name is required"):
        project_from_inputs("")
    with pytest.raises(ProjectError, match="collides with the chassis"):
        project_from_inputs("korodex")
    with pytest.raises(ProjectError, match="unknown ecosystem"):
        project_from_inputs("demo_app", ecosystem="cobol")


def test_ecosystem_key_accepts_keys_and_labels() -> None:
    assert ecosystem_key("python") == "python"
    assert ecosystem_key("FULLSTACK") == "fullstack"
    assert ecosystem_key(ECOSYSTEM_LABELS[1]) == "fullstack"
    assert ecosystem_key("mainframe") == ""


def test_generated_python_files_compile(tmp_path: Path) -> None:
    project = project_from_inputs("compile_check", ecosystem="fullstack")
    files = build_files(project)  # type: ignore[arg-type]
    sources = [name for name in files if name.endswith(".py")]
    assert sources
    for name in sources:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(files[name], encoding="utf-8")
        py_compile.compile(str(target), doraise=True)


def test_generated_configuration_files_parse(tmp_path: Path) -> None:
    project = project_from_inputs("parse_check")
    files = build_files(project)  # type: ignore[arg-type]
    pyproject = tomllib.loads(files["pyproject.toml"])
    assert pyproject["project"]["name"] == "parse-check"
    assert pyproject["project"]["dependencies"][0].startswith("korodexios-core")
    assert pyproject["project"]["scripts"]["parse-check"] == "parse_check.cli:main"
    config = yaml.safe_load(files["korodex.yml"])
    assert config["core"]["modules"]["extra_paths"] == ["src/parse_check/modules"]
    assert config["core"]["ipc"]["port"] == 0
    assert "\t" in files["Makefile"]
    assert files[".env.example"].startswith("# Env layer")


def test_minimal_variant_disables_the_example_modules(tmp_path: Path) -> None:
    project = project_from_inputs("bare_things", minimal=True)
    result = init_project(tmp_path / "bare_things", project)  # type: ignore[arg-type]
    assert result.root.name == "bare_things"
    assert not (result.root / "src/bare_things/modules/app").exists()
    assert (result.root / "src/bare_things/modules/.gitkeep").is_file()
    config = yaml.safe_load((result.root / "korodex.yml").read_text(encoding="utf-8"))
    assert config["core"]["modules"]["disabled"] == list(EXAMPLE_MODULES)
    assert "modules" not in config


def test_fullstack_variant_adds_a_web_client(tmp_path: Path) -> None:
    project = project_from_inputs("web_thing", ecosystem="fullstack")
    result = init_project(tmp_path / "web_thing", project)  # type: ignore[arg-type]
    for relative in (
        "web/index.html",
        "web/package.json",
        "web/src/main.js",
        "web/src/styles.css",
        ".agents/skills/web-ui.md",
    ):
        assert (result.root / relative).is_file(), relative
    script = (result.root / "web/src/main.js").read_text(encoding="utf-8")
    assert "X-Korodex-Token" in script
    assert "api/v1/status" in script
    skills = sorted((result.root / ".agents/skills").glob("*.md"))
    assert [skill.name for skill in skills] == [
        "cli-wizard-ux.md",
        "project-setup.md",
        "verification.md",
        "web-ui.md",
    ]


def test_init_project_refuses_a_busy_directory_without_force(tmp_path: Path) -> None:
    target = tmp_path / "taken"
    target.mkdir()
    (target / "notes.txt").write_text("mine", encoding="utf-8")
    project = project_from_inputs("demo_app")
    with pytest.raises(ProjectError, match="already holds files") as error:
        init_project(target, project)  # type: ignore[arg-type]
    assert error.value.hint
    result = init_project(target, project, force=True)  # type: ignore[arg-type]
    assert (result.root / "notes.txt").is_file()
    assert (result.root / "pyproject.toml").is_file()


def test_init_command_creates_a_project(capsys, tmp_path: Path) -> None:
    target = tmp_path / "cli_project"
    args = [
        "--home",
        str(tmp_path),
        "--no-log-file",
        "--no-color",
        "--yes",
        "init",
        "cli_project",
        "--path",
        str(target),
        "--author",
        "Kleo",
    ]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "project cli_project created" in captured.out
    assert "project cli_project created" in captured.out
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "Kleo"' in pyproject
    assert (target / "src/cli_project/cli.py").is_file()
    assert (target / "src/cli_project/modules/app/module.py").is_file()


def test_init_command_rejects_a_bad_name_without_prompting(capsys, tmp_path: Path) -> None:
    args = [
        "--home",
        str(tmp_path),
        "--no-log-file",
        "--no-color",
        "--yes",
        "init",
        "9lives",
        "--path",
        str(tmp_path / "nine"),
    ]
    assert main(args) == int(ExitCode.ERROR)
    captured = capsys.readouterr()
    assert "is not a usable project name" in captured.out
    assert "korodex init my_service" in captured.out


def test_init_command_is_offered_by_the_wizard(project_root) -> None:
    paths = AppPaths.discover(root=str(project_root), environ={})
    chassis = Chassis.create(
        ChassisOptions(home=paths.root, environ={}, cli_overrides={"core.log.file_enabled": False})
    )
    commands = {command.name: command for command in core_commands(chassis)}
    assert "init" in commands
    assert commands["init"].show_in_wizard is True
    assert commands["init"].group == "core"


def test_generated_project_passes_its_own_checks(tmp_path: Path) -> None:
    """The seed must be runnable: ruff, mypy, and pytest pass inside the new project."""
    target = tmp_path / "seeded"
    args = [
        "--home",
        str(tmp_path),
        "--no-log-file",
        "--no-color",
        "--yes",
        "init",
        "seeded",
        "--path",
        str(target),
        "--ecosystem",
        "python",
    ]
    assert main(args) == int(ExitCode.SUCCESS)
    for tool, tool_args in (
        ("ruff", ["-m", "ruff", "check", "src", "tests"]),
        ("mypy", ["-m", "mypy"]),
        ("pytest", ["-m", "pytest", "-q", "--color", "no"]),
    ):
        completed = subprocess.run(
            [sys.executable, *tool_args],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, f"{tool}: {completed.stdout}\n{completed.stderr}"
