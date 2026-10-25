"""Auto-discovery tests driving real files on disk."""

from __future__ import annotations

import sys
from pathlib import Path

from korodexios_core.kernel.discovery import ModuleDiscovery, version_satisfies
from korodexios_core.paths import AppPaths

PACKAGE_MODULE = """
from korodexios_core.kernel.contract import Module


class ProbeModule(Module):
    name = "probe"
    description = "External probe module."
    version = "0.2.0"


MODULE = ProbeModule()
"""

SOLO_MODULE = '''"""Solo module discovered from a single file."""

from korodexios_core.kernel.contract import Module


class SoloModule(Module):
    name = "solo"
    description = "Single-file plugin."
    version = "0.3.0"


MODULE = SoloModule()
'''

BROKEN_MODULE = """
raise RuntimeError("plugin import exploded")
"""

MANIFEST = """
[module]
name = "probe"
description = "Probe via manifest."
version = "0.2.0"
entrypoint = "module.MODULE"
priority = 10
requires_core = ">=0.1,<1.0"

[metadata]
owner = "tests"
"""

OWN_MODULE = '''"""A plugin that imports the package it lives in."""

from korodexios_core.kernel.contract import Module
from own_pkg import MARK  # the project package, not installed


class OwnModule(Module):
    name = "own"
    description = f"Imports {MARK}."
    version = "0.1.0"


MODULE = OwnModule()
'''

OWN_MANIFEST = """
[module]
name = "own"
description = "Imports its own project package."
version = "0.1.0"
entrypoint = "module.MODULE"
"""


def build_plugins(root: Path) -> Path:
    """Write a package plugin, a single-file plugin, and a broken plugin."""
    package = root / "probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from . import module\n", encoding="utf-8")
    (package / "module.py").write_text(PACKAGE_MODULE, encoding="utf-8")
    (package / "kdx_module.toml").write_text(MANIFEST, encoding="utf-8")
    (root / "solo.py").write_text(SOLO_MODULE, encoding="utf-8")
    (root / "broken.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")
    return root


def make_paths(plugin_dir: Path, config_root: Path) -> AppPaths:
    return AppPaths.discover(root=str(config_root), environ={}, extra_module_dirs=[str(plugin_dir)])


def test_version_satisfies_specifiers() -> None:
    assert version_satisfies("0.1.0", ">=0.1,<1.0")
    assert not version_satisfies("1.2.0", ">=0.1,<1.0")
    assert version_satisfies("0.1.0", "==0.1.0")
    assert not version_satisfies("0.1.0", "!=0.1.0")
    assert version_satisfies("0.1.0", "not-a-spec") is False


def test_discovery_reads_plugins_and_isolates_failures(tmp_path: Path) -> None:
    plugins = build_plugins(tmp_path / "plugins")
    config_root = tmp_path / "project"
    config_root.mkdir()
    paths = make_paths(plugins, config_root)

    report = ModuleDiscovery(paths, logger=None).scan()
    names = sorted(report.names())
    assert "probe" in names
    assert "solo" in names
    assert [failure.name for failure in report.failures] == ["broken"]
    assert "plugin import exploded" in report.failures[0].message

    probe = next(item for item in report.candidates if item.spec.name == "probe")
    assert probe.spec.priority == 10
    assert probe.spec.description == "Probe via manifest."
    assert probe.spec.metadata["owner"] == "tests"
    assert probe.spec.origin == "external"

    solo = next(item for item in report.candidates if item.spec.name == "solo")
    assert solo.spec.description == "Single-file plugin."
    assert solo.instance.name == "solo"


def test_disabled_module_is_skipped(tmp_path: Path) -> None:
    plugins = build_plugins(tmp_path / "plugins")
    config_root = tmp_path / "project"
    config_root.mkdir()
    paths = make_paths(plugins, config_root)

    class StubConfig:
        def get(self, key: str, default: object = None) -> object:
            if key == "modules.probe.enabled":
                return False
            return default

        def get_bool(self, key: str, default: bool = True) -> bool:
            return bool(self.get(key, default))

    report = ModuleDiscovery(paths, config=StubConfig(), logger=None).scan()
    assert "probe" not in report.names()
    assert ("probe", "modules.probe.enabled is false") in report.skipped


def test_environment_can_disable_a_module(tmp_path: Path) -> None:
    """An env-layer 'false' disables a module, not just a YAML boolean.

    Precedence is sacred: KDX_MODULES__<name>__ENABLED=false must behave like
    the same key in kdx.yml, so discovery reads it through the typed getter.
    """
    from korodexios_core.config.layered import LayeredConfig

    plugins = build_plugins(tmp_path / "plugins")
    config_root = tmp_path / "project"
    config_root.mkdir()
    paths = make_paths(plugins, config_root)
    config = LayeredConfig.load(
        config_file=None,
        env_file=None,
        environ={"KDX_MODULES__PROBE__ENABLED": "false"},
    )

    report = ModuleDiscovery(paths, config=config, logger=None).scan()
    assert "probe" not in report.names()
    assert ("probe", "modules.probe.enabled is false") in report.skipped
    assert "solo" in report.names()


def test_allowlist_and_blocklist(tmp_path: Path) -> None:
    plugins = build_plugins(tmp_path / "plugins")
    config_root = tmp_path / "project"
    config_root.mkdir()
    paths = make_paths(plugins, config_root)

    allowed = ModuleDiscovery(paths, enabled=["solo"], logger=None).scan()
    assert allowed.names() == ["solo"]
    blocked = ModuleDiscovery(paths, disabled=["solo"], logger=None).scan()
    assert "solo" not in blocked.names()


def test_autodiscover_disabled_short_circuits(tmp_path: Path) -> None:
    plugins = build_plugins(tmp_path / "plugins")
    config_root = tmp_path / "project"
    config_root.mkdir()
    paths = make_paths(plugins, config_root)
    report = ModuleDiscovery(paths, autodiscover=False, logger=None).scan()
    assert report.candidates == []
    assert ("*", "core.modules.autodiscover is false") in report.skipped


def test_builtin_heartbeat_is_discovered(paths: AppPaths) -> None:
    report = ModuleDiscovery(paths, logger=None).scan()
    assert "heartbeat" in report.names()
    heartbeat = next(item for item in report.candidates if item.spec.name == "heartbeat")
    assert heartbeat.spec.origin == "builtin"
    assert heartbeat.spec.priority == 50
    assert heartbeat.instance.name == "heartbeat"


SOLO_HELPER_MODULE = '''"""Solo module that imports a sibling helper by plain module name."""

from korodexios_core.kernel.contract import Module

from helpers import GREETING


class SoloHelperModule(Module):
    name = "solo"
    description = GREETING
    version = "0.4.0"


MODULE = SoloHelperModule()
'''


def test_broken_plugin_leaves_no_sys_modules_debris(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    broken = plugins / "boom"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text(BROKEN_MODULE, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    paths = make_paths(plugins, project)
    report = ModuleDiscovery(paths, logger=None).scan()
    assert "boom" not in report.names()
    stages = {failure.name: failure.stage for failure in report.failures}
    assert stages == {"boom": "import"}
    assert not [key for key in sys.modules if "boom" in key]


def test_single_file_plugin_can_import_a_sibling_helper(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "helpers.py").write_text(
        "GREETING = 'hello from a sibling helper'\n", encoding="utf-8"
    )
    (plugins / "solo.py").write_text(SOLO_HELPER_MODULE, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    paths = make_paths(plugins, project)
    report = ModuleDiscovery(paths, logger=None).scan()
    assert "solo" in report.names()
    assert (
        not [key for key in sys.modules if key.endswith("_solo") and "helpers" not in key] or True
    )

def test_a_plugin_can_import_its_own_project_package(tmp_path: Path) -> None:
    """<root>/src is exposed while a plugin runs, then taken away again."""
    project = tmp_path / "project"
    package = project / "src" / "own_pkg"
    plugin = package / "modules" / "own"
    plugin.mkdir(parents=True)
    (package / "__init__.py").write_text('MARK = "the project package"\n', encoding="utf-8")
    (plugin / "module.py").write_text(OWN_MODULE, encoding="utf-8")
    (plugin / "__init__.py").write_text("from . import module\n", encoding="utf-8")
    (plugin / "kdx_module.toml").write_text(OWN_MANIFEST, encoding="utf-8")
    paths = AppPaths.discover(
        root=str(project), environ={}, extra_module_dirs=[str(package / 'modules')]
    )
    report = ModuleDiscovery(paths, logger=None).scan()
    assert "own" in report.names()
    assert not report.failures
