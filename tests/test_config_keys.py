"""Configuration key rules: what the chassis reads, and what a typo means."""

from __future__ import annotations

import pytest

from korodexios_core.config import CORE_DEFAULTS
from korodexios_core.config.keys import (
    core_keys,
    is_core_key,
    is_known_key,
    is_project_key,
    module_of,
    module_scope,
    project_key_names,
    suggest,
    unknown_keys,
)

MODULES = ("heartbeat", "backup_engine")


def test_core_keys_are_the_default_table() -> None:
    """Every built-in default is a chassis key, and nothing else is."""
    assert core_keys() == tuple(sorted(CORE_DEFAULTS))
    assert is_core_key("core.log.level")
    assert not is_core_key("core.lg.level")


def test_a_typo_is_unknown_with_the_closest_key() -> None:
    """A near miss names the key the operator meant."""
    assert suggest("core.lg.level", MODULES) == "core.log.level"
    assert suggest("core.ipc.prot", MODULES) == "core.ipc.port"
    assert unknown_keys(["core.lg.level"], MODULES) == [("core.lg.level", "core.log.level")]


def test_module_scope_follows_discovery() -> None:
    """A module key is known only while that module exists, except its settings."""
    assert module_of("modules.heartbeat.interval") == "heartbeat"
    assert module_scope("app") == "modules.app"
    assert is_known_key("modules.heartbeat.interval", MODULES)
    assert not is_known_key("modules.greeter.greeting", MODULES)
    assert is_known_key("modules.greeter.enabled", MODULES)  # read before it is discovered
    assert is_known_key("modules.greeter.priority", MODULES)


def test_a_missing_module_suggests_the_installed_one() -> None:
    """The hint points at the module the operator probably meant."""
    assert suggest("modules.heartbeatt.interval", MODULES) == "modules.heartbeat"


def test_project_keys_stay_quiet() -> None:
    """A project may keep its own keys next to the chassis keys."""
    assert is_project_key("greeting")
    assert is_project_key("project.owner")
    assert is_project_key("app.name")  # a dotted project namespace is still project-owned
    assert not is_project_key("core.log.level")
    assert not is_project_key("modules.app.enabled")
    keys = ["greeting", "project.owner", "app.name", "core.log.level", "modules.app.enabled"]
    assert project_key_names(keys) == ("app.name", "greeting", "project.owner")
    assert unknown_keys(keys, MODULES) == []


@pytest.mark.parametrize("key", ["core.lg.level", "modules.greeter.greeting"])
def test_only_the_two_confident_classes_are_reported(key: str) -> None:
    """Foreign keys are the project's business, so they are never an error."""
    assert not is_known_key(key, MODULES)
    assert [entry[0] for entry in unknown_keys([key], MODULES)] == [key]


@pytest.mark.parametrize("key", ["cor.log.level", "moduls.app.enabled"])
def test_an_almost_spelled_namespace_is_a_suspect(key: str) -> None:
    """A head that almost spells core or modules is a typo, not a project key."""
    assert not is_project_key(key)
    assert not is_known_key(key, MODULES)

