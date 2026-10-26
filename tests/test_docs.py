"""The documentation suite exists, mirrors itself, and documents what the code does."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from korodexios_core import CliRequest, ModuleState
from korodexios_core.cli.init_project import ProjectTemplate, build_files
from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.kernel.contract import check_contract
from korodexios_core.paths import AppPaths

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

REQUIRED_DOCS = {
    "AGENTS_GUIDE.md": 200,
    "ARCHITECTURE.md": 110,
    "CLI_REFERENCE.md": 110,
    "TUTORIAL.md": 90,
    "ROADMAP.md": 30,
}


def test_docs_name_the_released_version() -> None:
    """A tag ships docs that name its own version, never the previous one."""
    source = read(REPO_ROOT / "src/korodexios_core/version.py")
    match = re.search('__version__: Final\\[str\\] = "([^"]+)"', source)
    assert match is not None, "version.py must keep the annotated assignment"
    version = match.group(1)
    assert f"**Release:** `v{version}`" in read(REPO_ROOT / "README.md")
    guide = read(DOCS / "AGENTS_GUIDE.md")
    assert f"| Version | `{version}`" in guide
    assert f"| Version | `{version}`" in read(REPO_ROOT / "llms.txt")
    assert f"@v{version}" in read(DOCS / "CLI_REFERENCE.md")
    assert f"## [{version}]" in read(REPO_ROOT / "CHANGELOG.md")


def read(path: Path) -> str:
    """Return *path* as UTF-8 text."""
    return path.read_text(encoding="utf-8")


def probe_project(minimal: bool = False) -> ProjectTemplate:
    """Return a throwaway project template for seed assertions."""
    return ProjectTemplate(
        name="probe",
        package="probe",
        distribution="probe",
        title="Probe",
        author="Korodexios",
        ecosystem="python",
        minimal=minimal,
        description="Probe chassis.",
    )


class RecordingSurface:
    """CliSurface double that records what a module contributes."""

    def __init__(self) -> None:
        self.commands: list[Any] = []

    def add_command(self, command: Any) -> None:
        self.commands.append(command)


def test_changelog_exists_and_leads_with_unreleased() -> None:
    changelog = read(REPO_ROOT / "CHANGELOG.md")
    assert changelog.startswith("# Changelog"), "CHANGELOG.md must lead with its title"
    assert "## [Unreleased]" in changelog, "the current change must be recorded"
    assert "## [0.1.0]" in changelog, "the released version must be recorded"


def test_documentation_suite_exists() -> None:
    for name, minimum in REQUIRED_DOCS.items():
        path = DOCS / name
        assert path.is_file(), f"missing docs/{name}"
        lines = read(path).splitlines()
        assert len(lines) >= minimum, f"docs/{name} is too thin: {len(lines)} lines"
        assert lines[0].startswith("# "), f"docs/{name} has no title"


def test_llms_txt_mirrors_the_agent_guide() -> None:
    """llms.txt is a byte-identical mirror, so the two can never drift."""
    # (kept in sync by this test; agents may read either copy)
    guide = read(DOCS / "AGENTS_GUIDE.md")
    mirror = read(REPO_ROOT / "llms.txt")
    assert mirror == guide
    assert len(guide) > 12_000


def test_agent_guide_is_contract_first() -> None:
    guide = read(DOCS / "AGENTS_GUIDE.md")
    for needle in (
        "## 1. Golden Rules of the Chassis",
        "Headless first",
        "Zero stubs",
        "Stdlib first",
        "## 3. Canonical Module Blueprint",
        "class MetricsModule(Module):",
        "self.config.get_float",
        "self.logger.info",
        "CliCommand(",
        "CliArgument(",
        "threading.Thread",
        "self.bus.publish",
        "self.bus.subscribe",
        "def health(self)",
        "kdx_module.toml",
        "requires_core",
        "entrypoint",
        "depends_on",
        "make check",
        "kdx run --once",
        "kdx status",
    ):
        assert needle in guide, f"agent guide lost {needle!r}"


def test_agent_guide_documents_the_verification_loop() -> None:
    """The exact sequence, in order, is stated as a contract."""
    guide = read(DOCS / "AGENTS_GUIDE.md")
    section = guide.split("## 12. Verification Loop", 1)[1].split("## 13.", 1)[0]
    order = [section.index(needle) for needle in ("make check", "kdx run --once", "kdx status")]
    assert order == sorted(order)


def test_cli_reference_covers_every_command_and_lan_option() -> None:
    reference = read(DOCS / "CLI_REFERENCE.md")
    for needle in (
        "kdx run",
        "kdx status",
        "kdx logs",
        "kdx init",
        "kdx modules",
        "kdx config",
        "kdx doctor",
        "kdx wizard",
        "kdx version",
        "kdx system",
        "kdx heartbeat",
        "kdx backup",
        "--target",
        "--host 0.0.0.0",
        "--no-auth",
        "--ipc-token",
        "KDX_CORE__IPC__TOKEN",
        "130",
        "143",
    ):
        assert needle in reference, f"CLI reference lost {needle!r}"


def test_architecture_explains_the_layers_and_planes() -> None:
    architecture = read(DOCS / "ARCHITECTURE.md")
    for needle in (
        "kdx.local.yml",
        "KDX_",
        "chassis.ipc",
        "X-Kdx-Token",
        "SIGTERM",
        "tick_interval",
        "stop_timeout",
        "max_depth",
        "DiscoveryReport",
    ):
        assert needle in architecture, f"architecture lost {needle!r}"


def test_tutorial_ends_with_a_running_module() -> None:
    tutorial = read(DOCS / "TUTORIAL.md")
    for needle in (
        "kdx init",
        "kdx modules create",
        "kdx_module.toml",
        "MODULE",
        "make check",
        "docs/AGENTS_GUIDE.md",
    ):
        assert needle in tutorial, f"tutorial lost {needle!r}"


def test_roadmap_records_the_phase_history_and_the_next_step() -> None:
    """The roadmap is a real document, not a README section that rots."""
    roadmap = read(DOCS / "ROADMAP.md")
    for needle in ("## Phases", "## Next", "| 1 |", "| 12 |", "make check"):
        assert needle in roadmap, f"roadmap lost {needle!r}"
    next_section = roadmap.split("## Next", 1)[1].split("## Release", 1)[0]
    for needle in ("Publish to an index", "Textual/TUI", "Windows in CI"):
        assert needle in next_section, f"roadmap next lost {needle!r}"


def test_readme_indexes_the_documentation_suite() -> None:
    readme = read(REPO_ROOT / "README.md")
    for target in (
        "docs/TUTORIAL.md",
        "docs/CLI_REFERENCE.md",
        "docs/ARCHITECTURE.md",
        "docs/AGENTS_GUIDE.md",
        "docs/ROADMAP.md",
        "llms.txt",
        "AGENTS.md",
    ):
        assert f"]({target})" in readme, f"README no longer links {target}"


def test_internal_links_resolve() -> None:
    """Every relative link in the suite points at a real file."""
    pages = [DOCS / name for name in REQUIRED_DOCS]
    pages += [REPO_ROOT / "README.md", REPO_ROOT / "llms.txt", REPO_ROOT / "AGENTS.md"]
    for page in pages:
        text = read(page)
        # llms.txt lives at the repo root but mirrors docs/AGENTS_GUIDE.md, whose
        # relative links are written for that canonical location.
        base = DOCS if page.name == "llms.txt" else page.parent
        for target in re.findall(r"\]\((?!https?://|#)([^)\s]+)\)", text):
            resolved = (base / target).resolve()
            assert resolved.exists(), f"{page.name} links to missing {target}"


def test_seed_ships_docs_for_agents_and_humans() -> None:
    files = build_files(probe_project())
    for name in ("docs/AGENTS_GUIDE.md", "docs/ARCHITECTURE.md"):
        assert name in files, f"kdx init no longer writes {name}"
        text = files[name]
        assert "probe" in text, f"{name} is not tailored to the child project"
        assert "@BT@" not in text, f"{name} still holds backtick placeholders"
        assert text.count("```") % 2 == 0, f"{name} has an unbalanced fence"
        assert len(text.splitlines()) > 90


def test_minimal_seed_keeps_the_docs() -> None:
    files = build_files(probe_project(minimal=True))
    assert "docs/AGENTS_GUIDE.md" in files
    assert "docs/ARCHITECTURE.md" in files
    assert "modules/app/" not in files["docs/ARCHITECTURE.md"]


def test_agent_guide_blueprint_runs(project_root: Path) -> None:
    """The blueprint in the agent guide is executable code, not pseudocode."""
    guide = read(DOCS / "AGENTS_GUIDE.md")
    blocks = re.findall(r"```python\n(.*?)```", guide, re.DOTALL)
    blueprint = next(block for block in blocks if "class MetricsModule" in block)
    namespace: dict[str, Any] = {"__name__": "kdx_blueprint"}
    exec(compile(blueprint, "AGENTS_GUIDE.md", "exec"), namespace)
    paths = AppPaths.discover(root=str(project_root), environ={})
    chassis = Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            cli_overrides={"core.log.file_enabled": False},
            install_signals=False,
        )
    )
    try:
        module = namespace["MetricsModule"]()
        assert check_contract(module) == []
        module.init(chassis.context)
        assert module.state is ModuleState.INITIALIZED
        surface = RecordingSurface()
        module.register_cli(surface)
        assert [command.name for command in surface.commands] == ["metrics"]
        module.start()
        assert module.state is ModuleState.RUNNING
        chassis.context.bus.publish("core.heartbeat", {"tick": 1}, source="test")
        health = module.health()
        assert health["name"] == "metrics"
        assert health["heartbeats_seen"] == 1  # the documented subscription is live
        module.stop()
        assert module.state is ModuleState.STOPPED
        chassis.context.bus.publish("core.heartbeat", {"tick": 2}, source="test")
        assert module.health()["heartbeats_seen"] == 1  # stop() unsubscribed
        request = CliRequest(context=chassis.context, command="metrics status")
        assert namespace["_status_command"](request) == 0
    finally:
        chassis.ensure_initialized().shutdown()


def test_agent_guide_manifest_example_is_valid_toml() -> None:
    """The manifest reference is copy-pasteable: the example parses."""
    import tomllib

    guide = read(DOCS / "AGENTS_GUIDE.md")
    block = next(
        block for block in re.findall(r"```toml\n(.*?)```", guide, re.DOTALL) if "[module]" in block
    )
    data = tomllib.loads(block)
    assert data["module"]["entrypoint"] == "MODULE"
    assert data["module"]["name"] == "metrics"
    assert "metadata" in data


def test_bootstrap_leads_with_uv_and_the_new_keys_are_documented() -> None:
    """uv (Astral) owns the virtualenv, and the v0.2.10 switches are written down."""
    readme = read(REPO_ROOT / "README.md")
    guide = read(DOCS / "AGENTS_GUIDE.md")
    reference = read(DOCS / "CLI_REFERENCE.md")
    for needle in (
        "uv venv",
        "uv sync --extra dev",
        "uv run kdx",
        "uv tool install korodexios-core",
        "uv pip install",
        "kdx-core.git@v",
        "uv lock",
        "python3 -m venv",
    ):
        assert needle in readme, f"the README lost the uv bootstrap: {needle!r}"
    assert readme.index("uv venv") < readme.index("python3 -m venv"), "uv must come first"
    for needle in ("uv run make check", "uv sync --extra dev", "EXTERNALLY-MANAGED"):
        assert needle in guide, f"the agent guide lost the uv bootstrap: {needle!r}"
    for needle in ("uv venv", "uv lock", "uv tool install"):
        assert needle in reference, f"the CLI reference lost the uv bootstrap: {needle!r}"
    for needle in (
        "core.modules.strict_discovery",
        "core.modules.foreign_roots",
        "core.env.aliases",
        "modules.<name>.enabled",
    ):
        assert needle in guide, f"the agent guide stopped documenting {needle!r}"
    for needle in ("strict_discovery", "foreign_roots", "settings_collisions"):
        assert needle in reference, f"the CLI reference stopped documenting {needle!r}"
    for needle in ("core.project.name", "core.config.sentinels", "contracts()"):
        assert needle in guide, (
            f"the agent guide stopped documenting the v0.3.0 surface: {needle!r}"
        )
    for needle in (
        "kdx contracts check",
        "core.project.contract_dir",
        "KDX_CORE__CONFIG__SENTINELS",
    ):
        assert needle in reference, (
            f"the CLI reference stopped documenting the v0.3.0 surface: {needle!r}"
        )
