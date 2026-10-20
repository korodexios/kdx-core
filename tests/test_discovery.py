"""Auto-discovery tests driving real files on disk."""

from __future__ import annotations

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


def build_plugins(root: Path) -> Path:
    """Write a package plugin, a single-file plugin, and a broken plugin."""
    package = root / "probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from . import module\n", encoding="utf-8")
    (package / "module.py").write_text(PACKAGE_MODULE, encoding="utf-8")
    (package / "korodex_module.toml").write_text(MANIFEST, encoding="utf-8")
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

    report = ModuleDiscovery(paths, config=StubConfig(), logger=None).scan()
    assert "probe" not in report.names()
    assert ("probe", "disabled by manifest or configuration") in report.skipped


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
