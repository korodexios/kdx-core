"""Declarative module configuration schemas and the findings they produce."""

from __future__ import annotations

from pathlib import Path

import pytest

from korodexios_core.config.layered import LayeredConfig, is_secret_key, redact_value
from korodexios_core.config.loaders import (
    apply_env_aliases,
    load_dotenv_layer,
    load_environment_layer,
    normalize_alias_mapping,
    parse_env_alias,
)
from korodexios_core.config.schema import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    ConfigField,
    ConfigIssue,
    ConfigKind,
    ConfigSchema,
    schema_from_mapping,
    summarize_issues,
)
from korodexios_core.exceptions import ConfigFileError, ConfigValueError

CONFIG_TEXT = """
core:
  log:
    level: INFO
    file_enabled: false
  ipc:
    enabled: false
modules:
  scripted:
    enabled: true
    port: 1500
    token: "s3cret"
"""


def write_config(root: Path, text: str = CONFIG_TEXT) -> LayeredConfig:
    """Write a throwaway kdx.yml and return the layered view over it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "kdx.yml").write_text(text, encoding="utf-8")
    return LayeredConfig.load(config_file=root / "kdx.yml", environ={})


def make_schema(module: str = "scripted") -> ConfigSchema:
    """Return a schema exercising coercion, choices, secrets, and required keys."""
    return ConfigSchema(
        module=module,
        fields=(
            ConfigField("port", ConfigKind.INT, default=1500, description="listen port"),
            ConfigField("token", ConfigKind.STR, secret=True, required=True),
            ConfigField("ratio", ConfigKind.FLOAT, default=0.5, choices=("0.5", "1.5")),
            ConfigField("enabled", ConfigKind.BOOL, default=True),
            ConfigField("paths", ConfigKind.LIST, default=()),
        ),
    )


def test_config_kinds_are_strings_and_are_validated() -> None:
    assert ConfigKind.STR == "str"
    assert ConfigKind("int") is ConfigKind.INT
    assert ConfigField("port", "int").kind is ConfigKind.INT
    with pytest.raises(ValueError):
        ConfigField("port", "nope")
    with pytest.raises(ValueError):
        ConfigField("  ", ConfigKind.STR)


def test_field_coercion_and_checks() -> None:
    field = ConfigField("port", ConfigKind.INT, default=1500, choices=("1500", "1600"))
    assert field.coerce("1600") == 1600
    assert field.check("1600") == ""
    assert field.check("1500") == ""
    assert "1600" in field.check("9999")
    with pytest.raises(ConfigValueError):
        field.coerce("not-a-number")
    assert field.check("not-a-number") != ""
    assert ConfigField("flag", ConfigKind.BOOL).coerce("yes") is True
    assert ConfigField("ratio", ConfigKind.FLOAT).coerce("1.5") == 1.5
    assert ConfigField("paths", ConfigKind.LIST).coerce("a,b") == ["a", "b"]
    assert ConfigField("when", ConfigKind.PATH).coerce("var/x") == Path("var/x")
    assert ConfigField("blob", ConfigKind.ANY).coerce({"a": 1}) == {"a": 1}
    assert ConfigField("blob", ConfigKind.ANY).check("anything") == ""


def test_field_as_dict_carries_declaration() -> None:
    field = ConfigField("token", ConfigKind.STR, secret=True, description="shared secret")
    payload = field.as_dict()
    assert payload["key"] == "token"
    assert payload["type"] == "str"
    assert payload["secret"] is True
    assert payload["description"] == "shared secret"
    assert payload["choices"] == []


def test_schema_helpers_report_shape() -> None:
    schema = make_schema()
    assert schema.keys() == ("port", "token", "ratio", "enabled", "paths")
    assert schema.secrets() == ("token",)
    assert schema.required_keys() == ("token",)
    assert schema.field_for("port") is not None
    assert schema.field_for("missing") is None
    assert not schema.is_empty
    assert ConfigSchema(module="empty").is_empty
    payload = schema.as_dict()
    assert payload["module"] == "scripted"
    assert len(payload["fields"]) == 5
    with pytest.raises(ValueError):
        ConfigSchema(module="dup", fields=(ConfigField("a"), ConfigField("a")))


def test_schema_validate_against_layered_config(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    assert make_schema().validate(config) == []
    broken = write_config(tmp_path, CONFIG_TEXT.replace("port: 1500", "port: not-a-port"))
    findings = make_schema().validate(broken)
    assert [issue.key for issue in findings] == ["modules.scripted.port"]
    assert findings[0].severity == SEVERITY_ERROR
    assert findings[0].module == "scripted"


def test_schema_required_field_without_default_is_reported() -> None:
    schema = ConfigSchema(
        module="scripted",
        fields=(ConfigField("never-set", ConfigKind.STR, required=True),),
    )
    issues = schema.validate(values={"other": "x"}, origins={"never-set": "file"})
    assert len(issues) == 1
    assert issues[0].key == "modules.scripted.never-set"
    assert issues[0].severity == SEVERITY_ERROR
    assert "required" in issues[0].message
    assert issues[0].describe() == (
        "scripted: never-set is required by module scripted and has no default [file]"
    )


def test_schema_choice_violation_is_an_error() -> None:
    schema = ConfigSchema(
        module="scripted",
        fields=(ConfigField("ratio", ConfigKind.FLOAT, default=0.5, choices=("0.5", "1.5")),),
    )
    issues = schema.validate(values={"ratio": 2.5})
    assert issues and "must be one of" in issues[0].message
    assert schema.validate(values={"ratio": 1.5}) == []


def test_schema_from_mapping_accepts_manifest_shapes() -> None:
    from_mapping = schema_from_mapping(
        {"port": {"kind": "int", "default": 1500}, "token": "str"},
        module="scripted",
    )
    assert from_mapping.keys() == ("port", "token")
    from_sequence = schema_from_mapping(
        [{"key": "port", "type": "int"}, {"key": "token", "type": "str", "secret": True}],
        module="scripted",
    )
    assert from_sequence.secrets() == ("token",)
    from_fields = schema_from_mapping(
        {"fields": [{"key": "port", "type": "int"}, {"key": "token", "type": "str"}]},
        module="scripted",
    )
    assert from_fields.keys() == ("port", "token")
    assert schema_from_mapping("not-a-schema", module="scripted").is_empty
    with pytest.raises(ValueError):
        schema_from_mapping({"port": 3}, module="scripted")


def test_issue_and_summary_shapes() -> None:
    issue = ConfigIssue(
        "modules.x.port",
        "needs an int",
        severity=SEVERITY_WARNING,
        source="file",
        module="x",
    )
    assert issue.describe() == "x: needs an int [file]"
    assert issue.as_dict()["severity"] == "warning"
    assert ConfigIssue("k", "boom").describe() == "boom"
    summary = summarize_issues(
        [
            ConfigIssue("a", "boom"),
            ConfigIssue("b", "hmm", severity=SEVERITY_WARNING),
            ConfigIssue("c", "boom"),
            ConfigIssue("d", "fyi", severity=SEVERITY_INFO),
        ]
    )
    assert summary == {"error": 2, "warning": 1, "info": 1}
    assert summarize_issues([]) == {"error": 0, "warning": 0, "info": 0}


def test_secret_detection_and_redaction() -> None:
    assert is_secret_key("core.ipc.token")
    assert is_secret_key("modules.backup.aws_access_key_id")
    assert is_secret_key("modules.x.api_key")
    assert is_secret_key("modules.x.password")
    assert not is_secret_key("core.ipc.auth")
    assert not is_secret_key("core.log.level")
    assert not is_secret_key("modules.backup.destination")
    assert redact_value("hunter2") == "***redacted***"
    assert redact_value("") == ""
    assert redact_value(1500) == 1500
    assert redact_value(None) is None
    assert redact_value("x", marker="***") == "***"


def test_layered_config_masks_secrets(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    assert config.is_secret("modules.scripted.token")
    assert not config.is_secret("modules.scripted.port")
    assert "modules.scripted.token" in config.secret_keys()
    assert config.as_dict()["modules.scripted.token"] == "s3cret"
    assert config.as_dict(redact=True)["modules.scripted.token"] == "***redacted***"
    masked = {row[0]: row[1] for row in config.describe_rows(redact=True)}
    assert masked["modules.scripted.token"] == "***redacted***"
    plain = {row[0]: row[1] for row in config.describe_rows()}
    assert plain["modules.scripted.token"] == "s3cret"


def test_env_alias_parsing_and_application() -> None:
    assert parse_env_alias("LEGACY_TOKEN=core.ipc.token") == ("LEGACY_TOKEN", "core.ipc.token")
    assert parse_env_alias("legacy-token=core.ipc.token") == ("LEGACY_TOKEN", "core.ipc.token")
    assert parse_env_alias("nonsense") is None
    assert parse_env_alias("CORE_IPC_TOKEN=core.ipc.token") is None
    assert normalize_alias_mapping("LEGACY=core.ipc.token") == {"LEGACY": "core.ipc.token"}
    assert normalize_alias_mapping(["A=core.a", "B=core.b"]) == {"A": "core.a", "B": "core.b"}
    assert normalize_alias_mapping({"A": "core.a"}) == {"A": "core.a"}
    assert normalize_alias_mapping(None) == {}
    applied = apply_env_aliases(
        [("LEGACY_TOKEN", "abc"), ("CORE__NAME", "probe")],
        {"LEGACY_TOKEN": "core.ipc.token"},
    )
    assert applied == {"core.ipc.token": ("abc", "environment alias LEGACY_TOKEN")}


def test_environment_layer_reports_alias_provenance() -> None:
    values, origins = load_environment_layer(
        {"KDX_CORE__NAME": "prefixed", "LEGACY_TOKEN": "abc"},
        aliases={"LEGACY_TOKEN": "core.ipc.token", "KDX_CORE__NAME": "core.name"},
    )
    assert values["core.name"] == "prefixed"
    assert "core.name" not in origins
    assert values["core.ipc.token"] == "abc"
    assert origins["core.ipc.token"] == "environment alias LEGACY_TOKEN"


def test_dotenv_layer_reports_alias_provenance(tmp_path: Path) -> None:
    from korodexios_core.config.loaders import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nLEGACY_TOKEN = 'abc'\nCORE__IPC__PORT=18123\n"
        "KDX_CORE__IPC__PORT=19123\nBROKEN=\n",
        encoding="utf-8",
    )
    values, origins = load_dotenv_layer(env_file, aliases={"LEGACY_TOKEN": "core.ipc.token"})
    assert values["core.ipc.token"] == "abc"
    assert values["core.ipc.port"] == "19123"
    assert origins["core.ipc.token"] == "environment alias LEGACY_TOKEN"
    assert "core.name" not in values
    assert load_dotenv(env_file, aliases={"LEGACY_TOKEN": "core.ipc.token"})["core.ipc.port"] == (
        "19123"
    )
    bad = tmp_path / "broken.env"
    bad.write_text("not a pair\n", encoding="utf-8")
    with pytest.raises(ConfigFileError):
        load_dotenv_layer(bad)
