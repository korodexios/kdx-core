"""v0.3.0: the project triple, a verified file set, and declared sentinels."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import korodexios_core
from korodexios_core import (
    Chassis,
    ChassisOptions,
    ContractDrift,
    ContractReport,
    ContractRow,
    ProjectIdentity,
    contract_rows,
    project_identity,
    verify_contracts,
)
from korodexios_core.cli.entry import main
from korodexios_core.config.layered import ConfigSource, LayeredConfig
from korodexios_core.config.schema import ConfigField, ConfigKind, ConfigSchema
from korodexios_core.constants import ExitCode
from korodexios_core.version import __version__

PROJECT_KEYS = """
core:
  project:
    name: agentic-librarian
    version: 4.2.0
    contract_dir: Schema
"""

SENTINELS = """
core:
  log:
    level: INFO
    file_enabled: false
  config:
    sentinels:
      - inherit
      - '<unset>'
"""

PROBE_SOURCE = '''
from korodexios_core.kernel.contract import Module


class Prober(Module):
    name = "prober"
    description = "Declares one contract file of its own."
    version = "1.0.0"

    def contracts(self) -> tuple[dict[str, str], ...]:
        return ({"path": "prober.json", "sha256": "0" * 64, "classification": "kit_owned"},)

    def config_schema(self) -> dict[str, str]:
        return {"threshold": "float"}


MODULE = Prober()
'''

PROBE_MANIFEST = """
[module]
name = "prober"
description = "Declares one contract file of its own."
version = "1.0.0"
entrypoint = "MODULE"
"""


def base_args(root: Path) -> list[str]:
    """Return the global flags every CLI test in this file needs."""
    return ["--home", str(root), "--no-log-file", "--no-color"]


def digest(text: str) -> str:
    """Return the hex SHA-256 of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(captured: str) -> dict[str, Any]:
    """Return the JSON payload a CLI command printed."""
    return json.loads(captured)


def write_probe(root: Path) -> Path:
    """Write one module package that declares a contract file, and return its root."""
    directory = root / "modules" / "prober"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(PROBE_SOURCE.lstrip(), encoding="utf-8")
    (directory / "kdx_module.toml").write_text(PROBE_MANIFEST.lstrip(), encoding="utf-8")
    return root / "modules"


# R3: one project identity, read from configuration.


def test_project_identity_reads_the_project_triple(tmp_path: Path) -> None:
    """A project that states itself gets exactly the triple it stated."""
    config_file = tmp_path / "kdx.yml"
    config_file.write_text(PROJECT_KEYS.lstrip(), encoding="utf-8")
    config = LayeredConfig.load(config_file=config_file, env_file=None, environ={})
    identity = project_identity(config, home=tmp_path)
    assert identity.name == "agentic-librarian"
    assert identity.version == "4.2.0"
    assert identity.contract_dir == str(tmp_path / "Schema")
    assert identity.as_dict() == {
        "name": "agentic-librarian",
        "version": "4.2.0",
        "contract_dir": str(tmp_path / "Schema"),
    }


def test_project_identity_falls_back_to_what_the_chassis_knows(tmp_path: Path) -> None:
    """Without project keys the chassis answers from keys it already owns."""
    config = LayeredConfig.load(config_file=None, env_file=None, environ={})
    identity = project_identity(config, home=tmp_path)
    assert identity.name == config.get_str("core.name")
    assert identity.version == __version__
    assert identity.contract_dir == str(tmp_path / "contracts")
    assert isinstance(identity, ProjectIdentity)


def test_doctor_reports_the_same_triple_the_chassis_holds(
    tmp_path: Path,
    capsys,
) -> None:
    """The identity is one source: health, doctor text, and doctor JSON agree."""
    (tmp_path / "kdx.yml").write_text(PROJECT_KEYS.lstrip(), encoding="utf-8")
    chassis = Chassis.create(ChassisOptions(home=tmp_path, environ={}, install_signals=False))
    try:
        chassis.ensure_initialized()
        identity = chassis.project
        assert identity is not None
        assert chassis.health_report()["project"] == identity.as_dict()
    finally:
        chassis.shutdown()
    assert main([*base_args(tmp_path), "doctor"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "Project identity" in captured.out
    assert "agentic-librarian" in captured.out
    capsys.readouterr()
    assert main([*base_args(tmp_path), "doctor", "--json"]) == int(ExitCode.SUCCESS)
    report = load_json(capsys.readouterr().out)
    assert report["project"] == {
        "name": "agentic-librarian",
        "version": "4.2.0",
        "contract_dir": str(tmp_path / "Schema"),
    }


# R5: a declared sentinel falls through instead of setting a value.


def test_a_declared_sentinel_falls_through_to_the_layer_below(tmp_path: Path) -> None:
    """A layer that says 'inherit' has no opinion, so the file below answers."""
    config_file = tmp_path / "kdx.yml"
    config_file.write_text(SENTINELS.lstrip(), encoding="utf-8")
    local_file = tmp_path / "kdx.local.yml"
    local_file.write_text("core:\n  log:\n    level: inherit\n", encoding="utf-8")
    config = LayeredConfig.load(
        config_file=config_file, local_config_file=local_file, env_file=None, environ={}
    )
    assert config.get_str("core.log.level") == "INFO"
    assert config.source_of("core.log.level") is ConfigSource.INHERIT
    assert config.source_of("core.log.level").label() == "inherit sentinel"  # type: ignore[union-attr]
    origin = config.origin_of("core.log.level")
    assert "inherit sentinel" in origin and "falls through to" in origin


def test_the_chassis_invents_no_sentinel_of_its_own(tmp_path: Path) -> None:
    """With no tokens declared, 'inherit' is an ordinary value that wins."""
    config_file = tmp_path / "kdx.yml"
    config_file.write_text("core:\n  log:\n    level: inherit\n", encoding="utf-8")
    config = LayeredConfig.load(config_file=config_file, env_file=None, environ={})
    assert config.sentinel_tokens() == ()
    assert config.get_str("core.log.level") == "inherit"
    assert config.source_of("core.log.level") is ConfigSource.FILE


def test_one_variable_can_carry_the_sentinel_tokens(tmp_path: Path) -> None:
    """An operator can export the set instead of editing a file."""
    config = LayeredConfig.load(
        config_file=None,
        env_file=None,
        environ={"KDX_CORE__CONFIG__SENTINELS": "inherit, <unset>"},
    )
    assert config.sentinel_tokens() == ("<unset>", "inherit")
    assert config.get_str("core.log.level") == "INFO"


def test_a_sentinel_with_nothing_below_is_reported_unset(tmp_path: Path) -> None:
    """A key that only ever says 'inherit' is not a value anyone set."""
    config_file = tmp_path / "kdx.yml"
    config_file.write_text(
        SENTINELS.lstrip() + "modules:\n  prober:\n    threshold: inherit\n",
        encoding="utf-8",
    )
    config = LayeredConfig.load(config_file=config_file, env_file=None, environ={})
    config.declare_sentinels({"modules.prober.threshold": "inherit"})
    assert config.get("modules.prober.threshold") is None
    assert config.source_of("modules.prober.threshold") is ConfigSource.INHERIT
    rows = {row[0]: (row[1], row[2]) for row in config.describe_rows()}
    assert rows["modules.prober.threshold"] == ("(unset)", "inherit sentinel")


def test_a_module_schema_can_declare_its_own_sentinel() -> None:
    """A module states the token per key, and validation stops calling it a bad value."""
    schema = ConfigSchema(
        module="prober",
        fields=(
            ConfigField(key="threshold", kind=ConfigKind.FLOAT, default=0.4, sentinel="inherit"),
        ),
    )
    assert schema.sentinels() == {"threshold": "inherit"}
    assert schema.validate(values={"threshold": "inherit"}) == []
    issues = schema.validate(values={"threshold": "dense"})
    assert issues and issues[0].key == "modules.prober.threshold"


# R4: a verified file set the chassis reads and never rewrites.


def test_verification_hashes_files_and_never_writes_one(tmp_path: Path) -> None:
    """A matching set verifies; a byte changed by the project reports drift."""
    (tmp_path / "kit.json").write_text("{\"a\": 1}", encoding="utf-8")
    rows = (ContractRow(path="kit.json", sha256=digest("{\"a\": 1}"), classification="kit_owned"),)
    before = (tmp_path / "kit.json").read_bytes()
    report = verify_contracts(rows, tmp_path)
    assert report.ok and report.verified == ("kit.json",)
    assert (tmp_path / "kit.json").read_bytes() == before
    (tmp_path / "kit.json").write_text("{\"a\": 2}", encoding="utf-8")
    drifted = verify_contracts(rows, tmp_path)
    assert not drifted.ok and len(drifted.drifted) == 1
    finding = drifted.drifted[0]
    assert isinstance(finding, ContractDrift)
    assert finding.path == "kit.json"
    assert finding.declared == digest("{\"a\": 1}")
    assert finding.actual == digest("{\"a\": 2}")
    assert drifted.missing == ()


def test_a_declared_file_that_is_not_there_is_missing(tmp_path: Path) -> None:
    """A declared file the operator never installed is a finding, not a crash."""
    report = verify_contracts((ContractRow(path="gone.json", sha256="ab"),), tmp_path)
    assert report.missing == ("gone.json",)
    assert not report.ok
    assert isinstance(report, ContractReport)
    assert report.summary() == "1 declared, 0 match, 1 missing"


def test_a_hook_may_answer_with_mappings_or_nothing() -> None:
    """The chassis normalizes what a contracts() hook returns, in any of its shapes."""
    rows = contract_rows(
        [
            {"path": "a.json", "sha256": "aa", "classification": "kit_owned"},
            {"path": "b.json", "sha256": "", "merge_policy": "replace_if_pristine"},
            {"sha256": "cc"},
            ContractRow(path="d.json", sha256="dd"),
        ]
    )
    assert [row.path for row in rows] == ["a.json", "b.json", "d.json"]
    assert rows[1].merge_policy == "replace_if_pristine"
    assert contract_rows(None) == ()


def test_a_module_hook_reaches_the_report_and_the_doctor(
    tmp_path: Path,
    capsys,
) -> None:
    """A module that declares a file lands in health, doctor, and the exit code."""
    (tmp_path / "kdx.yml").write_text(
        """
core:
  log:
    level: INFO
    file_enabled: false
  modules:
    autodiscover: true
    extra_paths: []
  lifecycle:
    tick_interval: 0.05
    stop_timeout: 2.0
  ipc:
    enabled: false
""".lstrip(),
        encoding="utf-8",
    )
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "prober.json").write_text("{\"kept\": true}", encoding="utf-8")
    plugins = write_probe(tmp_path)
    args = [*base_args(tmp_path), "--modules-dir", str(plugins)]
    assert main([*args, "contracts", "check"]) == int(ExitCode.CONFIG_ERROR)
    captured = capsys.readouterr()
    assert "prober.json" in captured.err
    assert "never rewrites" in captured.err
    capsys.readouterr()
    assert main([*args, "contracts", "check", "--json"]) == int(ExitCode.CONFIG_ERROR)
    payload = load_json(capsys.readouterr().out)
    assert payload["declared"] == 1
    assert payload["declared_by"]["prober"][0]["path"] == "prober.json"
    assert payload["drifted"][0]["actual"] == digest("{\"kept\": true}")
    assert main([*args, "doctor"]) == int(ExitCode.SUCCESS)
    capsys.readouterr()
    assert main([*args, "doctor", "--json"]) == int(ExitCode.SUCCESS)
    report = load_json(capsys.readouterr().out)
    checks = [
        check for check in report["diagnostics"]["checks"] if check["name"] == "contracts"
    ]
    assert checks and checks[0]["severity"] == "warning"


def test_the_project_can_register_its_own_rows(tmp_path: Path) -> None:
    """An embedding app states rows once; the chassis keeps them and dedupes paths."""
    (tmp_path / "kdx.yml").write_text(
        """
core:
  log:
    level: INFO
    file_enabled: false
  modules:
    autodiscover: false
    extra_paths: []
  lifecycle:
    tick_interval: 0.05
    stop_timeout: 2.0
  ipc:
    enabled: false
  project:
    name: agentic-librarian
    version: 4.2.0
    contract_dir: contracts
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "kit.json").write_text("kit", encoding="utf-8")
    chassis = Chassis.create(
        ChassisOptions(
            home=tmp_path,
            environ={},
            install_signals=False,
            project_contracts=(
                ContractRow(path="kit.json", sha256=digest("kit"), merge_policy="keep"),
                ContractRow(path="kit.json", sha256="deadbeef"),
                ContractRow(path="gone.json", sha256=digest("gone")),
            ),
        )
    )
    try:
        chassis.ensure_initialized()
        assert len(chassis.project_contracts) == 2
        report = chassis.contract_report()
        assert report.verified == ("kit.json",)
        assert report.missing == ("gone.json",)
        assert [name for name, _ in chassis.declared_contracts()] == ["project", "project"]
        added = chassis.register_contracts(
            [{"path": "extra.json", "sha256": "", "classification": "project_owned"}]
        )
        assert len(added) == 3
        assert not chassis.contract_report().ok
    finally:
        chassis.shutdown()


# R8 and the frozen surface: the new names are part of the package root.


def test_the_new_names_are_exported_from_the_package_root() -> None:
    """The chassis v3 surface is importable from the package root, not a submodule."""
    for name in (
        "ProjectIdentity",
        "project_identity",
        "ContractRow",
        "ContractDrift",
        "ContractReport",
        "contract_rows",
        "verify_contracts",
    ):
        assert name in korodexios_core.__all__, name + " is not exported"
        assert getattr(korodexios_core, name) is not None

