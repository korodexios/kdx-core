"""Scaffolding tests: generated packages compile, load, and satisfy the contract."""

from __future__ import annotations

import importlib.util
import py_compile
from pathlib import Path

import pytest

from korodexios_core.cli.scaffold import (
    ScaffoldError,
    pascal_case,
    scaffold_module,
    validate_module_name,
)
from korodexios_core.kernel.contract import check_contract


def project(tmp_path: Path) -> Path:
    """Return a chassis-like project root with a modules dir and a tests dir."""
    root = tmp_path / "project"
    (root / "korodexios_core" / "modules").mkdir(parents=True)
    (root / "tests").mkdir()
    return root


def test_pascal_case_matches_module_names() -> None:
    assert pascal_case("backup_engine") == "BackupEngine"
    assert pascal_case("a") == "A"
    assert pascal_case("core_api_v2") == "CoreApiV2"


def test_validate_module_name_accepts_and_rejects() -> None:
    assert validate_module_name(" backup_engine ") == "backup_engine"
    for bad in ("", "BadName", "9lives", "with-dash", "two words"):
        with pytest.raises(ScaffoldError):
            validate_module_name(bad)


def test_scaffold_writes_a_working_package(tmp_path: Path) -> None:
    root = project(tmp_path)
    modules = root / "korodexios_core" / "modules"
    result = scaffold_module(modules, "demo_plugin", title="Demo plugin.", project_root=root)
    assert [item.name for item in result.created] == [
        "korodex_module.toml",
        "module.py",
        "__init__.py",
    ]
    assert result.test_file is not None and result.test_file.name == "test_demo_plugin.py"
    for item in result.created:
        py_compile.compile(str(item), doraise=True)
    spec = importlib.util.spec_from_file_location("scaffolded_demo", result.directory / "module.py")
    assert spec is not None and spec.loader is not None
    module_obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module_obj)
    assert check_contract(module_obj.MODULE) == []
    assert module_obj.MODULE.name == "demo_plugin"
    assert module_obj.MODULE.health()["reports"] == 0


def test_scaffold_refuses_existing_module_without_force(tmp_path: Path) -> None:
    root = project(tmp_path)
    modules = root / "korodexios_core" / "modules"
    scaffold_module(modules, "demo_plugin", project_root=root)
    with pytest.raises(ScaffoldError, match="already holds a module") as excinfo:
        scaffold_module(modules, "demo_plugin", project_root=root)
    assert excinfo.value.hint
    replaced = scaffold_module(modules, "demo_plugin", force=True, project_root=root)
    assert replaced.replaced is True


def test_scaffold_outside_the_package_skips_the_test_file(tmp_path: Path) -> None:
    root = project(tmp_path)
    plugins = tmp_path / "my_plugins"
    result = scaffold_module(plugins, "loose_plugin", project_root=root)
    assert result.test_file is None
    assert result.directory == plugins / "loose_plugin"
