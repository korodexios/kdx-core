"""Configuration hierarchy, coercion, and provenance tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from korodexios_core.config.defaults import CORE_DEFAULTS
from korodexios_core.config.layered import ConfigSource, LayeredConfig, ScopedConfig
from korodexios_core.config.loaders import (
    flatten_mapping,
    load_config_file,
    load_dotenv,
    normalize_env_key,
    unflatten_mapping,
)
from korodexios_core.exceptions import (
    ConfigFileError,
    ConfigurationError,
    ConfigValueError,
)

FILE_CONFIG = """
core:
  log:
    level: WARNING
    max_bytes: 2048
modules:
  scripted:
    enabled: false
    interval: 0.25
"""


def test_flatten_round_trip() -> None:
    nested = {"core": {"log": {"level": "INFO"}}, "modules": {"a": {"enabled": True}}}
    flat = flatten_mapping(nested)
    assert flat == {"core.log.level": "INFO", "modules.a.enabled": True}
    assert unflatten_mapping(flat)["core"]["log"]["level"] == "INFO"


def test_normalize_env_key_rules() -> None:
    assert normalize_env_key("KORODEX_CORE__LOG__LEVEL") == "core.log.level"
    assert normalize_env_key("KORODEX_CORE__LOG__FILE_ENABLED") == "core.log.file_enabled"
    assert normalize_env_key("KORODEX_MODULES__HEARTBEAT__INTERVAL") == "modules.heartbeat.interval"
    assert normalize_env_key("PATH") is None
    assert normalize_env_key("KORODEX_") is None


def test_load_config_file_yaml(tmp_path: Path) -> None:
    target = tmp_path / "korodex.yml"
    target.write_text(FILE_CONFIG, encoding="utf-8")
    values = load_config_file(target)
    assert values["core.log.level"] == "WARNING"
    assert values["core.log.max_bytes"] == 2048
    assert values["modules.scripted.enabled"] is False


def test_load_config_file_json(tmp_path: Path) -> None:
    target = tmp_path / "korodex.json"
    target.write_text('{"core": {"log": {"level": "ERROR"}}}', encoding="utf-8")
    assert load_config_file(target)["core.log.level"] == "ERROR"


def test_load_config_file_rejects_scalar(tmp_path: Path) -> None:
    target = tmp_path / "korodex.yml"
    target.write_text("just a string", encoding="utf-8")
    with pytest.raises(ConfigFileError):
        load_config_file(target)


def test_load_dotenv_skips_comments_and_quotes(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text(
        "# comment\nexport KORODEX_CORE__LOG__LEVEL=DEBUG\n"
        'KORODEX_CORE__LOG__FILE="korodex.log"  # inline comment\n'
        "SHELL=/bin/sh\n",
        encoding="utf-8",
    )
    values = load_dotenv(target)
    assert values["core.log.level"] == "DEBUG"
    assert values["core.log.file"] == "korodex.log"
    assert "shell" not in values


def test_precedence_cli_beats_env_beats_file_beats_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "korodex.yml"
    config_file.write_text(FILE_CONFIG, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("KORODEX_CORE__LOG__LEVEL=INFO\n", encoding="utf-8")

    config = LayeredConfig.load(
        config_file=config_file,
        env_file=env_file,
        environ={"KORODEX_CORE__LOG__LEVEL": "WARNING", "KORODEX_CORE__NAME": "acme"},
        cli_overrides={"core.log.level": "CRITICAL"},
    )
    assert config.get("core.log.level") == "CRITICAL"
    assert config.source_of("core.log.level") is ConfigSource.CLI
    assert config.get("core.name") == "acme"
    assert config.source_of("core.name") is ConfigSource.ENV
    assert config.get("core.log.max_bytes") == 2048
    assert config.source_of("core.log.max_bytes") is ConfigSource.FILE
    assert config.get("core.log.backup_count") == CORE_DEFAULTS["core.log.backup_count"]
    assert config.source_of("core.log.backup_count") is ConfigSource.DEFAULT


def test_real_environment_beats_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KORODEX_CORE__LOG__LEVEL=DEBUG\n", encoding="utf-8")
    config = LayeredConfig.load(
        config_file=None, env_file=env_file, environ={"KORODEX_CORE__LOG__LEVEL": "ERROR"}
    )
    assert config.get("core.log.level") == "ERROR"


def test_typed_coercion_and_errors(config: LayeredConfig) -> None:
    assert config.get_bool("core.log.file_enabled") is False
    assert config.get_float("core.lifecycle.tick_interval") == pytest.approx(0.05)
    assert config.get_int("core.log.backup_count") == 5
    assert config.get_str("core.name") == "korodexios"
    assert config.get_list("core.modules.extra_paths") == []
    broken = LayeredConfig.load(
        config_file=None, env_file=None, environ={"KORODEX_CORE__LOG__MAX_BYTES": "wide"}
    )
    with pytest.raises(ConfigValueError) as resolved:
        broken.get_int("core.log.max_bytes")
    assert resolved.value.key == "core.log.max_bytes"


def test_require_unknown_key_raises(config: LayeredConfig) -> None:
    assert config.get("nope.not.defined", "fallback") == "fallback"
    with pytest.raises(ConfigurationError):
        config.require("nope.not.defined")


def test_scoped_config_prefers_prefixed_key() -> None:
    config = LayeredConfig.load(
        config_file=None,
        env_file=None,
        environ={},
        cli_overrides={
            "modules.scripted.interval": 1.5,
            "interval": 7.0,
            "stop_timeout": 9.0,
        },
    )
    scoped: ScopedConfig = config.child("modules.scripted")
    assert scoped.get_float("interval") == pytest.approx(1.5)  # prefixed key wins
    assert scoped.get_float("stop_timeout") == pytest.approx(9.0)  # bare global fallback
    assert scoped.get("missing", "default") == "default"
    assert "interval" in scoped
    assert scoped.prefix == "modules.scripted"
    assert scoped.as_dict()["interval"] == 1.5
    assert scoped.parent is config


def test_describe_rows_report_provenance(config: LayeredConfig) -> None:
    rows = {row[0]: (row[1], row[2]) for row in config.describe_rows()}
    assert rows["core.log.level"] == ("DEBUG", "environment")
    assert rows["core.log.file_enabled"] == ("false", "config file")
