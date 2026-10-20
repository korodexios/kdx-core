"""Naming alignment: config candidates, environment prefixes, and the local override layer."""

from __future__ import annotations

from pathlib import Path

from korodexios_core.config.defaults import CORE_DEFAULTS
from korodexios_core.config.layered import ConfigSource, LayeredConfig
from korodexios_core.config.loaders import env_prefixes, load_environment, normalize_env_key
from korodexios_core.constants import (
    CONFIG_CANDIDATES,
    ENV_PREFIX,
    ENV_PREFIX_ALIASES,
    HOME_ENV_VARS,
    LOCAL_CONFIG_CANDIDATES,
    ROOT_MARKER_FILES,
)
from korodexios_core.paths import AppPaths

PRIMARY_YAML = """
core:
  name: primary-name
  log:
    level: INFO
"""

LEGACY_YAML = """
core:
  name: legacy-name
"""

LOCAL_YAML = """
core:
  log:
    level: DEBUG
"""


def write(path: Path, text: str) -> Path:
    """Write *text* to *path* as UTF-8 and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_candidate_order_leads_with_the_project_names() -> None:
    """kdx.* first, korodexios.* second; no other aliases are candidates."""
    assert CONFIG_CANDIDATES[:3] == ("kdx.yml", "kdx.yaml", "kdx.json")
    assert CONFIG_CANDIDATES[3:6] == (
        "korodexios.yml",
        "korodexios.yaml",
        "korodexios.json",
    )
    assert len(CONFIG_CANDIDATES) == 6
    assert LOCAL_CONFIG_CANDIDATES[0] == "kdx.local.yml"
    assert "korodexios.local.yml" in LOCAL_CONFIG_CANDIDATES
    assert len(LOCAL_CONFIG_CANDIDATES) == 6
    assert ".kdx-root" in ROOT_MARKER_FILES
    assert "kdx.yml" in ROOT_MARKER_FILES


def test_environment_prefix_defaults_to_the_short_alias() -> None:
    """KDX_ is primary; KORODEXIOS_ is the canonical long prefix."""
    assert ENV_PREFIX == "KDX"
    assert ENV_PREFIX_ALIASES == ("KDX", "KORODEXIOS")
    assert CORE_DEFAULTS["core.env.prefix"] == "KDX"
    assert normalize_env_key("KDX_CORE__LOG__LEVEL") == "core.log.level"
    assert normalize_env_key("KORODEXIOS_CORE__LOG__LEVEL") == "core.log.level"
    assert normalize_env_key("KORODEX_CORE__LOG__LEVEL") is None
    assert normalize_env_key("KDX_") is None
    assert normalize_env_key("PATH") is None


def test_a_private_prefix_does_not_inherit_the_aliases() -> None:
    """core.env.prefix: MYAPP means exactly MYAPP_, never the built-in aliases."""
    assert env_prefixes("MYAPP") == ("MYAPP",)
    assert normalize_env_key("KDX_CORE__LOG__LEVEL", "MYAPP") is None
    assert normalize_env_key("MYAPP_CORE__LOG__LEVEL", "MYAPP") == "core.log.level"
    assert env_prefixes(None) == ENV_PREFIX_ALIASES


def test_primary_prefix_wins_over_the_canonical_one() -> None:
    values = load_environment(
        {
            "KORODEXIOS_CORE__IPC__HOST": "127.0.0.1",
            "KDX_CORE__IPC__HOST": "0.0.0.0",
            "KORODEXIOS_CORE__IPC__PORT": "8123",
        }
    )
    assert values["core.ipc.host"] == "0.0.0.0"
    assert values["core.ipc.port"] == "8123"


def test_chassis_discovers_kdx_yml(tmp_path: Path) -> None:
    write(tmp_path / "kdx.yml", PRIMARY_YAML)
    paths = AppPaths.discover(root=str(tmp_path), environ={})
    assert paths.config_file is not None
    assert paths.config_file.name == "kdx.yml"
    config = LayeredConfig.load(config_file=paths.config_file, env_file=None, environ={})
    assert config.get_str("core.name") == "primary-name"
    assert config.source_of("core.name") is ConfigSource.FILE


def test_kdx_yml_wins_over_korodexios_yml(tmp_path: Path) -> None:
    write(tmp_path / "kdx.yml", PRIMARY_YAML)
    write(tmp_path / "korodexios.yml", LEGACY_YAML)
    paths = AppPaths.discover(root=str(tmp_path), environ={})
    assert paths.config_file is not None
    assert paths.config_file.name == "kdx.yml"


def test_korodexios_yml_is_the_canonical_candidate(tmp_path: Path) -> None:
    write(tmp_path / "korodexios.yml", LEGACY_YAML)
    write(tmp_path / "korodex.yml", PRIMARY_YAML)  # inert: not a candidate
    paths = AppPaths.discover(root=str(tmp_path), environ={})
    assert paths.config_file is not None
    assert paths.config_file.name == "korodexios.yml"


def test_local_override_layer_sits_between_file_and_env(tmp_path: Path) -> None:
    write(tmp_path / "kdx.yml", PRIMARY_YAML)
    local = write(tmp_path / "kdx.local.yml", LOCAL_YAML)
    paths = AppPaths.discover(root=str(tmp_path), environ={})
    assert paths.local_config_file == local
    config = LayeredConfig.load(
        config_file=paths.config_file,
        local_config_file=paths.local_config_file,
        env_file=None,
        environ={},
    )
    assert config.get_str("core.log.level") == "DEBUG"
    assert config.source_of("core.log.level") is ConfigSource.LOCAL
    assert "kdx.local.yml" in config.origin_of("core.log.level")
    assert config.get_str("core.name") == "primary-name"  # untouched keys keep the file value
    assert paths.as_dict()["local_config_file"] == str(local)


def test_environment_beats_the_config_file(tmp_path: Path) -> None:
    config_file = write(tmp_path / "kdx.yml", PRIMARY_YAML)
    config = LayeredConfig.load(
        config_file=config_file,
        env_file=None,
        environ={"KDX_CORE__LOG__LEVEL": "DEBUG"},
    )
    assert config.get_str("core.log.level") == "DEBUG"
    assert config.source_of("core.log.level") is ConfigSource.ENV


def test_home_environment_variable_accepts_both_prefixes(tmp_path: Path) -> None:
    write(tmp_path / "kdx.yml", PRIMARY_YAML)
    for variable in HOME_ENV_VARS:
        paths = AppPaths.discover(root=None, environ={variable: str(tmp_path)})
        assert paths.root == tmp_path
        assert paths.config_file is not None and paths.config_file.name == "kdx.yml"


def test_local_override_is_optional(tmp_path: Path) -> None:
    write(tmp_path / "kdx.yml", PRIMARY_YAML)
    paths = AppPaths.discover(root=str(tmp_path), environ={})
    assert paths.local_config_file is None
    config = LayeredConfig.load(config_file=paths.config_file, env_file=None, environ={})
    assert config.source_of("core.log.level") is ConfigSource.FILE
    assert config.source_of("core.lifecycle.tick_interval") is ConfigSource.DEFAULT


def test_local_override_file_is_a_root_marker(tmp_path: Path) -> None:
    nested = tmp_path / "project" / "src" / "app"
    write(tmp_path / "project" / "kdx.local.yml", LOCAL_YAML)
    nested.mkdir(parents=True)
    paths = AppPaths.discover(root=None, environ={"KDX_HOME": str(nested)})
    assert paths.root == nested
    discovered = AppPaths.discover(root=str(nested), environ={})
    assert discovered.root.parent.parent == tmp_path / "project"
