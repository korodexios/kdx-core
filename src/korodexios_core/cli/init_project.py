"""Project templating: 'korodex init' seeds a runnable repository from the chassis.

The chassis exists to be the seed of future projects, so a new repository must be
one command away - not a copy-rename-replace session. Everything generated here
is real, importable, lint-clean code: no placeholder bodies, no empty functions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..validate import ValidationResult
from ..version import __version__ as CORE_VERSION
from .ux import UserExperience

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
RAW_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
RESERVED_NAMES = frozenset(
    {
        "build",
        "dist",
        "docs",
        "korodex",
        "korodexios",
        "korodexios_core",
        "src",
        "tests",
        "tools",
    }
)
DEFAULT_AUTHOR = "Korodexios"
EXAMPLE_MODULES = ("heartbeat", "backup_engine")
ECOSYSTEMS: dict[str, str] = {
    "python": "Python (uv-managed)",
    "fullstack": "Fullstack (Python Core + Node/Web UI)",
}
ECOSYSTEM_LABELS = tuple(ECOSYSTEMS[key] for key in ("python", "fullstack"))
IGNORED_WHEN_CHECKING = frozenset(
    {".git", ".idea", ".venv", ".ruff_cache", ".mypy_cache", ".pytest_cache"}
)


class ProjectError(ValueError):
    """Raised when a project name, target directory, or option is unusable."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True, slots=True)
class ProjectTemplate:
    """Everything the generators need to write one project."""

    name: str
    package: str
    distribution: str
    title: str
    author: str
    ecosystem: str
    minimal: bool
    description: str
    core_version: str = CORE_VERSION

    @property
    def script(self) -> str:
        """Return the console script name installed by the project."""
        return self.distribution

    @property
    def fullstack(self) -> bool:
        """Return True when the project also carries a web UI."""
        return self.ecosystem == "fullstack"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the template."""
        return {
            "name": self.name,
            "package": self.package,
            "distribution": self.distribution,
            "title": self.title,
            "author": self.author,
            "ecosystem": self.ecosystem,
            "minimal": self.minimal,
            "description": self.description,
            "core_version": self.core_version,
        }


@dataclass(slots=True)
class ProjectResult:
    """What one 'korodex init' run produced."""

    root: Path
    files: list[str] = field(default_factory=list)
    project: ProjectTemplate | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly summary of the generated project."""
        return {
            "root": str(self.root),
            "files": len(self.files),
            "paths": self.files,
            "project": self.project.as_dict() if self.project else None,
        }


def validate_project_name(text: str) -> ValidationResult:
    """Accept names that can become a Python package; return the package name."""
    raw = str(text).strip()
    if not raw:
        return ValidationResult.rejected("a project name", "a project name is required")
    if not RAW_NAME_PATTERN.match(raw):
        return ValidationResult.rejected(
            "letters, digits, '-' or '_', starting with a letter",
            f"{raw!r} is not a usable project name",
        )
    package = raw.lower().replace("-", "_")
    if package in RESERVED_NAMES:
        return ValidationResult.rejected(
            "a name that is not reserved by the chassis",
            f"{raw!r} collides with the chassis itself",
        )
    if not NAME_PATTERN.match(package):
        return ValidationResult.rejected("lower_case package name", f"{raw!r} cannot be a package")
    return ValidationResult.accepted(package, "lower_case package name")


def ecosystem_key(value: str) -> str:
    """Return the ecosystem key for a key or a label; empty when unknown."""
    raw = str(value).strip().lower()
    if raw in ECOSYSTEMS:
        return raw
    for key, label in ECOSYSTEMS.items():
        if label.lower() == str(value).strip().lower():
            return key
    return ""


def project_from_inputs(
    name: str,
    *,
    author: str = "",
    ecosystem: str = "",
    minimal: bool = False,
    description: str = "",
) -> ProjectTemplate:
    """Build a ProjectTemplate from CLI input, validating the name strictly."""
    validated = validate_project_name(name)
    if not validated.ok:
        raise ProjectError(
            f"{validated.message} (expected {validated.expected})",
            hint="use letters, digits, '-' or '_', starting with a letter, e.g. 'demo_app'",
        )
    package = str(validated.value)
    key = ecosystem_key(ecosystem)
    if ecosystem and not key:
        raise ProjectError(
            f"unknown ecosystem {ecosystem!r}", hint="choose 'python' or 'fullstack'"
        )
    key = key or "python"
    title = " ".join(part.capitalize() for part in package.split("_")) or package
    return ProjectTemplate(
        name=package,
        package=package,
        distribution=package.replace("_", "-"),
        title=title,
        author=str(author).strip() or DEFAULT_AUTHOR,
        ecosystem=key,
        minimal=bool(minimal),
        description=str(description).strip() or f"{title}: a korodexios chassis project.",
    )


def prompt_project(
    ux: UserExperience,
    *,
    name: str = "",
    author: str = "",
    ecosystem: str = "",
    minimal: bool = False,
) -> ProjectTemplate:
    """Ask the operator for the project identity, re-prompting on bad input."""
    validated = validate_project_name(name)
    while ux.is_interactive() and not validated.ok:
        answer = ux.ask_text(
            "Project name",
            example="my_service",
            validator=validate_project_name,
            required=True,
        )
        validated = validate_project_name(answer)
    if not validated.ok:
        raise ProjectError(
            f"{validated.message} (expected {validated.expected})",
            hint="pass the name: korodex init my_service",
        )
    package = str(validated.value)
    author_value = str(author).strip()
    if not author_value:
        author_value = ux.ask_text("Author / owner", default=DEFAULT_AUTHOR)
    ecosystem_value = ecosystem_key(ecosystem)
    if not ecosystem:
        chosen = ux.ask_choice("Target ecosystem", ECOSYSTEM_LABELS, default=ECOSYSTEM_LABELS[0])
        ecosystem_value = ecosystem_key(chosen) or "python"
    minimal_flag = minimal
    if not minimal_flag and ux.is_interactive():
        minimal_flag = ux.confirm("Skip the example modules for a bare chassis?", default=False)
    return project_from_inputs(
        package,
        author=author_value,
        ecosystem=ecosystem_value,
        minimal=minimal_flag,
    )


def init_project(target: Path, project: ProjectTemplate, *, force: bool = False) -> ProjectResult:
    """Write the project tree into *target*; refuse to overwrite without force."""
    destination = Path(target).expanduser()
    files = build_files(project)
    if destination.exists() and not force:
        existing = [
            entry.name for entry in destination.iterdir() if entry.name not in IGNORED_WHEN_CHECKING
        ]
        if existing:
            sample = ", ".join(sorted(existing)[:4])
            tail = ", ..." if len(existing) > 4 else ""
            raise ProjectError(
                f"{destination} already holds files ({sample}{tail})",
                hint="pass --force to write into it anyway",
            )
    destination.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for relative, content in sorted(files.items()):
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(relative)
    if project.minimal:
        keep = destination / "src" / project.package / "modules" / ".gitkeep"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("", encoding="utf-8")
        written.append(f"src/{project.package}/modules/.gitkeep")
    return ProjectResult(root=destination.resolve(), files=sorted(written), project=project)


def build_files(project: ProjectTemplate) -> dict[str, str]:
    """Return every file the generator writes, keyed by project-relative path."""
    package = project.package
    files: dict[str, str] = {
        "pyproject.toml": _pyproject(project),
        "README.md": _readme(project),
        "AGENTS.md": _agents(project),
        "docs/AGENTS_GUIDE.md": _docs_agents_guide(project),
        "docs/ARCHITECTURE.md": _docs_architecture(project),
        "Makefile": _makefile(project),
        "kdx.yml": _config_yml(project),
        ".gitignore": _gitignore(),
        ".env.example": _env_example(project),
        ".agents/skills/project-setup.md": _skill_project_setup(project),
        ".agents/skills/cli-wizard-ux.md": _skill_cli_wizard(project),
        ".agents/skills/verification.md": _skill_verification(project),
        f"src/{package}/__init__.py": _package_init(project),
        f"src/{package}/cli.py": _cli_source(project),
        "tests/test_smoke.py": _smoke_test(project),
    }
    if not project.minimal:
        files.update(
            {
                f"src/{package}/modules/app/__init__.py": _app_package(project),
                f"src/{package}/modules/app/module.py": _app_module(project),
                f"src/{package}/modules/app/korodex_module.toml": _app_manifest(project),
            }
        )
    if project.fullstack:
        files["web/package.json"] = _web_package(project)
        files["web/index.html"] = _web_index(project)
        files["web/src/main.js"] = _web_main(project)
        files["web/src/styles.css"] = _web_styles(project)
        files[".agents/skills/web-ui.md"] = _skill_web_ui(project)
    return files


def _pyproject(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            "[build-system]",
            'requires = ["hatchling>=1.24"]',
            'build-backend = "hatchling.build"',
            "",
            "[project]",
            f'name = "{project.distribution}"',
            'version = "0.1.0"',
            f'description = "{project.description}"',
            'requires-python = ">=3.11"',
            f'authors = [{{name = "{project.author}"}}]',
            "dependencies = [",
            "  # korodexios-core is not published yet: pip install -e ../korodexios-core",
            '  "korodexios-core>=0.1,<1.0",',
            "]",
            "",
            "[project.scripts]",
            f'"{project.script}" = "{project.package}.cli:main"',
            "",
            "[project.optional-dependencies]",
            'dev = ["hatchling>=1.24", "mypy>=1.10", "pytest>=8.0", "ruff>=0.6"]',
            "",
            "[tool.hatch.build.targets.wheel]",
            f'packages = ["src/{project.package}"]',
            "",
            "[tool.pytest.ini_options]",
            'pythonpath = ["src"]',
            'testpaths = ["tests"]',
            'addopts = "-q"',
            "",
            "[tool.ruff]",
            "line-length = 100",
            'src = ["src", "tests"]',
            "",
            "[tool.ruff.lint]",
            'select = ["E", "F", "W", "I", "UP", "B", "C4", "SIM", "RUF"]',
            "",
            "[tool.mypy]",
            'python_version = "3.11"',
            "strict = true",
            'mypy_path = "src"',
            f'packages = ["{project.package}"]',
            "",
        ]
    )


def _config_yml(project: ProjectTemplate) -> str:
    disabled = ", ".join(EXAMPLE_MODULES) if project.minimal else ""
    module_lines: list[str] = (
        []
        if project.minimal
        else [
            "modules:",
            "  app:",
            "    enabled: true",
            f'    greeting: "Hello from {project.title}"',
        ]
    )
    return "\n".join(
        [
            f"# {project.title}: chassis configuration (the config layer; CLI and env win).",
            "app:",
            f"  name: {project.name}",
            "core:",
            "  log:",
            "    level: INFO",
            "    file_enabled: true",
            "  modules:",
            "    autodiscover: true",
            "    extra_paths:",
            f"      - src/{project.package}/modules",
            f"    disabled: [{disabled}]",
            "  lifecycle:",
            "    tick_interval: 1.0",
            "    stop_timeout: 5.0",
            "  ipc:",
            "    enabled: true",
            "    host: 127.0.0.1    # 0.0.0.0 exposes the control plane to the LAN",
            "    port: 0            # ephemeral: never collides with a neighbour",
            "    timeout: 2.0",
            '    token: ""          # static shared secret; empty means one per boot',
            "    auth: true         # false = anonymous /health and /api/v1/status reads",
            "    allow_anonymous_shutdown: false  # keep POST /api/v1/shutdown guarded",
            *module_lines,
            "",
        ]
    )


def _readme(project: ProjectTemplate) -> str:
    fence = "@BT@@BT@@BT@"
    lines = [
        f"# {project.title}",
        "",
        f"{project.description} Built on the **korodexios-core** chassis: a headless",
        "microkernel that owns lifecycle, discovery, configuration, and clean shutdown,",
        "with this repository's modules on top.",
        "",
        "## Quickstart",
        "",
        fence + "bash",
        "uv sync --extra dev   # or: python -m venv .venv && source .venv/bin/activate",
        "uv run make check     # or: make check",
        f"{project.script} run            # boot the chassis (Ctrl-C exits 130)",
        f"{project.script} status         # query the running daemon",
        fence,
        "",
        "## Layout",
        "",
        fence + "text",
        f"src/{project.package}/",
        "  cli.py                 # entry point: boots the chassis with --home at this root",
    ]
    if project.minimal:
        lines += ["  modules/               # drop module packages here; discovery finds them"]
    else:
        lines += [
            "  modules/app/           # your first module (auto-discovered)",
            "    module.py            # name, description, version, init/register_cli/start/stop",
            "    korodex_module.toml  # manifest read by the discovery layer",
        ]
    lines += [
        "tests/",
        "  test_smoke.py          # proves discovery, config, and the module contract",
        "kdx.yml                  # project configuration (the config layer)",
        ".env.example             # copy to .env for the env layer",
        "AGENTS.md                # working agreement for humans and coding agents",
        "docs/AGENTS_GUIDE.md      # the contract, for humans and AI agents",
        "docs/ARCHITECTURE.md       # how this project is wired",
        ".agents/skills/          # playbooks: how to extend this project safely",
    ]
    if project.fullstack:
        lines += [
            "web/",
            "  index.html, src/       # static client: talks to the daemon over the IPC endpoint",
        ]
    lines += [fence, "", "## Configuration precedence", ""]
    lines += [
        "@BT@CLI flags > .env (KDX_*) > kdx.yml > built-in defaults@BT@: every key is",
        "dotted (core.log.level) and @BT@kdx doctor@BT@ shows where each value came from.",
        "",
        "## Verification",
        "",
        "@BT@make check@BT@ runs ruff, mypy (strict), and pytest. Keep it green.",
        "",
    ]
    if project.fullstack:
        lines += [
            "## Web UI",
            "",
            "The daemon binds 127.0.0.1 (set @BT@core.ipc.host: 0.0.0.0@BT@ for the LAN) and",
            "writes @BT@var/chassis.ipc@BT@ with pid, port, host, and token.",
            "Serve @BT@web/@BT@ with @BT@npm --prefix web run dev@BT@; open @BT@?port=&token=@BT@.",
            "",
        ]
    return "\n".join(lines).replace("@BT@", chr(96))


def _docs_agents_guide(project: ProjectTemplate) -> str:
    package = project.package
    fence = "@BT@@BT@@BT@"
    lines = [
        f"# {project.title} - Agent Guide",
        "",
        f"Contract-first reference for anyone editing {project.distribution}: humans and",
        "autonomous coding agents. The chassis contract is defined by korodexios-core;",
        "this file states what it means in this repository. Read it before editing.",
        "",
        "## Facts",
        "",
        "| Fact | Value |",
        "| --- | --- |",
        f"| Distribution | @BT@{project.distribution}@BT@ |",
        f"| Import root | @BT@{project.package}@BT@ (src layout) |",
        f"| Console script | @BT@{project.script}@BT@ |",
        f"| Chassis | korodexios-core >= @BT@{project.core_version}@BT@ |",
        "| Config file | @BT@kdx.yml@BT@ (local override: @BT@kdx.local.yml@BT@) |",
        "| Env prefix | @BT@KDX_@BT@ (@BT@KORODEXIOS_@BT@ and legacy @BT@KORODEX_@BT@ also read) |",
        f"| Module root | @BT@src/{package}/modules/@BT@ (auto-discovered) |",
        "| State dir | @BT@var/@BT@ (logs, @BT@chassis.ipc@BT@); never committed |",
        "| Chassis surface | the @BT@korodexios_core@BT@ package root @BT@__all__@BT@ |",
        "",
        "## Golden Rules",
        "",
        "1. **Headless first.** The chassis owns lifecycle, discovery, configuration, and",
        "   shutdown; @BT@cli.py@BT@ stays thin.",
        "2. **Zero stubs.** No TODO bodies, no @BT@pass@BT@ placeholders, no mock returns.",
        "3. **Stdlib first.** A new dependency needs a paragraph of justification.",
        "4. **Precedence is sacred.** CLI > env (@BT@KDX_@BT@, then @BT@KORODEXIOS_@BT@, then",
        "   legacy @BT@KORODEX_@BT@) > @BT@kdx.local.yml@BT@ > @BT@kdx.yml@BT@ > defaults.",
        "   Never read @BT@os.environ@BT@ in a module.",
        "5. **Diagnostics to stderr, UI to stdout.** Modules never print to stdout.",
        "6. **Clean shutdown.** Whatever @BT@start()@BT@ acquires, @BT@stop()@BT@ releases inside",
        "   @BT@core.lifecycle.stop_timeout@BT@. SIGINT exits 130, SIGTERM 143, an operator",
        "   stop 0.",
        "7. **Discovery is the extension point.** Add a module directory; do not edit the",
        "   chassis to add a feature.",
        "",
        "## Module Contract",
        "",
        "A module exposes seven members: @BT@name@BT@, @BT@description@BT@, @BT@version@BT@,",
        "@BT@init(context)@BT@, @BT@register_cli(surface)@BT@, @BT@start()@BT@, @BT@stop()@BT@.",
        "Subclass @BT@korodexios_core.Module@BT@ and you inherit them plus @BT@state@BT@,",
        "@BT@config@BT@, @BT@logger@BT@, @BT@bus@BT@, @BT@paths@BT@, and @BT@health()@BT@.",
        "Identity is validated at construction, so a missing @BT@name@BT@,",
        "@BT@description@BT@, or @BT@version@BT@ raises @BT@ModuleContractError@BT@",
        "immediately.",
        "",
        "## Blueprint",
        "",
        f"Copy this into @BT@src/{package}/modules/<name>/module.py@BT@ and rename the class.",
        "It is executable, not pseudocode: the project smoke tests boot a chassis.",
        "",
        fence + "python",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from korodexios_core import CliArgument, CliCommand, CliRequest, CoreContext, Module",
        "",
        "",
        "class GreeterModule(Module):",
        '    """Smallest honest module: scoped config, logging, CLI, health."""',
        "",
        '    name = "greeter"',
        '    description = "Greets on demand and proves the contract end to end."',
        '    version = "0.1.0"',
        "",
        "    def __init__(self) -> None:",
        "        super().__init__()",
        '        self._greeting = "Hello"',
        "        self._greeted = 0",
        "",
        "    def init(self, context: CoreContext) -> None:",
        '        """Read the scoped config; keys here mean modules.greeter.*."""',
        "        super().init(context)",
        '        self._greeting = self.config.get_str("greeting", "Hello")',
        '        self.logger.debug("greeter configured", greeting=self._greeting)',
        "",
        "    def register_cli(self, surface) -> None:",
        '        """Contribute \'<script> greeter hello\' to the CLI and the wizard."""',
        "        surface.add_command(",
        "            CliCommand(",
        '                name="greeter",',
        '                help="Greeter module utilities.",',
        "                module_name=self.name,",
        "                subcommands=(",
        "                    CliCommand(",
        '                        name="hello",',
        '                        help="Greet once and count the greeting.",',
        "                        module_name=self.name,",
        "                        handler=self._greet,",
        "                        args=(",
        "                            CliArgument(",
        '                                ("--who",),',
        '                                dest="who",',
        '                                type="str",',
        '                                default="korodexios",',
        '                                help="Who to greet.",',
        "                            ),",
        "                        ),",
        "                    ),",
        "                ),",
        "            )",
        "        )",
        "",
        "    def start(self) -> None:",
        '        """No background work to launch; the base marks the module RUNNING."""',
        "        super().start()",
        '        self.logger.info("greeter ready", greeting=self._greeting)',
        "",
        "    def stop(self) -> None:",
        '        """Release what start() acquired; here that is only bookkeeping."""',
        "        super().stop()",
        '        self.logger.info("greeter stopped", greeted=self._greeted)',
        "",
        "    def health(self) -> dict[str, Any]:",
        '        """Extend the lifecycle block with what an operator cares about."""',
        "        data = super().health()",
        '        data.update({"greeting": self._greeting, "greeted": self._greeted})',
        "        return data",
        "",
        "    def _greet(self, request: CliRequest) -> int:",
        '        """Handlers take a CliRequest and return a process exit code."""',
        '        who = str(request.option("who", "korodexios"))',
        "        self._greeted += 1",
        '        self.logger.info("greeting", who=who, greeting=self._greeting)',
        "        return 0",
        "",
        "",
        "MODULE = GreeterModule()  # the manifest entrypoint",
        fence,
        "",
        "## Module Manifest - @BT@korodex_module.toml@BT@",
        "",
        fence + "toml",
        "[module]",
        'name = "greeter"           # must equal the directory name',
        f"description = \"Greets on demand.\"   # shown by '{project.script} modules list'",
        'version = "0.1.0"          # MAJOR.MINOR.PATCH, validated at construction',
        'entrypoint = "MODULE"      # dotted path to the instance inside the package',
        "enabled = true             # false => state 'disabled', never started",
        "priority = 100             # int >= 0; lower starts earlier, ties by name",
        "depends_on = []            # names that must start before this module",
        'requires_core = ">=0.1"    # chassis gate; unmet => skipped with a reason',
        "",
        "[metadata]",
        'tags = ["example"]         # free-form; never validated, never required',
        fence,
        "",
        "- The manifest is optional: discovery falls back to the directory name and",
        "  @BT@MODULE@BT@.",
        "- A bad manifest, an import error, or a contract violation becomes a logged",
        "  discovery failure; the chassis keeps booting.",
        "- @BT@enabled@BT@ may also be turned off from @BT@kdx.yml@BT@ under",
        "  @BT@modules.<name>.enabled@BT@.",
        "",
        "## Configuration",
        "",
        "Five layers, lowest to highest: **defaults -> @BT@kdx.yml@BT@ -> @BT@kdx.local.yml@BT@ ->",
        "environment (@BT@.env@BT@ then process env) -> CLI overrides**.",
        "",
        "- Module keys live under @BT@modules.<name>@BT@ in @BT@kdx.yml@BT@; the scoped view",
        '  strips the prefix, so @BT@self.config.get_str("greeting", "Hello")@BT@ reads',
        "  @BT@modules.greeter.greeting@BT@.",
        "- Getters are typed: @BT@get_str@BT@, @BT@get_int@BT@, @BT@get_float@BT@,",
        "  @BT@get_bool@BT@, @BT@get_path@BT@, @BT@get_list@BT@; a coercion failure raises",
        "  @BT@ConfigValueError@BT@ naming key, value, and source.",
        f"- Provenance is per key: @BT@{project.script} config show@BT@ prints where every value",
        "  came from.",
        "- @BT@kdx.local.yml@BT@ is gitignored machine-local state; @BT@.env@BT@ is the",
        "  deployment layer.",
        "",
        "## Event Bus",
        "",
        "@BT@self.bus.subscribe(pattern, handler, name=..., once=False)@BT@,",
        "@BT@self.bus.publish(name, payload, source=self.name)@BT@,",
        "@BT@self.bus.wait_for(name, timeout=2.0)@BT@. Patterns are dot-segmented:",
        "@BT@*@BT@ matches one segment, @BT@**@BT@ matches any. Modules talk through events,",
        "never by importing each other.",
        "",
        "## Verification Loop",
        "",
        fence + "bash",
        "make check          # ruff, mypy --strict, pytest: the bar is 100% green",
        f"{project.script} run --once   # boot, one lifecycle tick, clean stop, exit 0",
        f"{project.script} status       # live report over the control plane",
        fence,
        "",
        "With a plain virtualenv, point make at its interpreter:",
        "@BT@make check PYTHON=.venv/bin/python@BT@. Quote the failing command before and",
        "after a fix, and add a test that fails without your change.",
        "",
        "## Do / Don't",
        "",
        "- **Do** put new behaviour in a module directory under @BT@src/"
        + package
        + "/modules/@BT@.",
        "- **Do** raise @BT@KorodexError@BT@ subclasses with a @BT@hint=@BT@ the CLI can print.",
        "- **Do** keep @BT@cli.py@BT@ thin: build @BT@ChassisOptions@BT@, call",
        "  @BT@Chassis.create@BT@, render.",
        "- **Don't** read @BT@os.environ@BT@, write to stdout, or spawn a thread @BT@stop()@BT@",
        "  cannot join.",
        "- **Don't** commit @BT@var/@BT@, @BT@.env@BT@, or @BT@kdx.local.yml@BT@.",
        "",
        "Chassis docs: @BT@docs/ARCHITECTURE.md@BT@ here, and the upstream",
        "@BT@docs/AGENTS_GUIDE.md@BT@ / @BT@llms.txt@BT@ in korodexios-core.",
    ]
    return "\n".join(lines).replace("@BT@", chr(96))


def _docs_architecture(project: ProjectTemplate) -> str:
    package = project.package
    fence = "@BT@@BT@@BT@"
    if project.minimal:
        module_line = "  modules/               # empty until you add one; discovery finds it"
    else:
        module_line = "  modules/app/           # the example module (auto-discovered)"
    lines = [
        f"# Architecture - {project.title}",
        "",
        f"{project.description}",
        "",
        "## What this project is",
        "",
        f"{project.title} is a chassis project, not a framework: **korodexios-core** owns",
        "lifecycle, discovery, configuration, and clean shutdown. This repository owns",
        "two things only - the module code and a thin CLI entry point.",
        "",
        "## How it is wired",
        "",
        fence + "text",
        f"{project.script}                  # console script installed by the project",
        "  v",
        f"src/{package}/cli.py      # thin: builds ChassisOptions(home=root) and runs the chassis",
        "  v",
        "korodexios_core.Chassis  # paths -> config -> logging -> bus -> discovery -> registry",
        "  v",
        f"src/{package}/modules/    # each directory: manifest + contract, imported by discovery",
        "  v",
        "init -> register_cli -> start -> lifecycle loop -> stop (reverse) -> close logging",
        fence,
        "",
        "Nothing in the chassis knows this project's name; nothing in this project",
        "reimplements a chassis concern.",
        "",
        "## Module roots",
        "",
        fence + "text",
        f"src/{package}/",
        "  cli.py                 # the only entry point",
        module_line,
        "  modules/<name>/",
        "    module.py            # the contract: init/register_cli/start/stop",
        "    korodex_module.toml  # manifest: name, version, entrypoint, priority, gates",
        "    __init__.py          # re-exports MODULE",
        fence,
        "",
        f"- @BT@src/{package}/modules/@BT@ is the discovery root; add a directory, not a",
        "  chassis patch.",
        "- Extra roots go in @BT@core.modules.extra_paths@BT@ in @BT@kdx.yml@BT@, or",
        "  @BT@--modules-dir@BT@.",
        "- Startup order is dependency topology first, then ascending @BT@priority@BT@;",
        "  shutdown reverses it.",
        "",
        "## Configuration flow",
        "",
        fence + "text",
        "  defaults               CORE_DEFAULTS from the chassis",
        "      v",
        "  kdx.yml                project config (korodexios.yml / korodex.yml also read)",
        "      v",
        "  kdx.local.yml          gitignored machine-local override",
        "      v",
        "  environment            .env, then process env (KDX_ > KORODEXIOS_ > KORODEX_)",
        "      v",
        "  CLI overrides          --log-level, --ipc-host, --ipc-port, --auth ...",
        fence,
        "",
        "- Module keys live under @BT@modules.<name>@BT@ in @BT@kdx.yml@BT@; the scoped view",
        "  hands the module the subtree with the prefix stripped.",
        f"- @BT@{project.script} config show@BT@ prints the winning value and its source per key.",
        "- Secrets come from the environment layer, never from @BT@os.environ@BT@ in",
        "  module code.",
        "",
        "## Control plane",
        "",
        "- The daemon binds @BT@127.0.0.1:8123@BT@ by default; set",
        "  @BT@core.ipc.host: 0.0.0.0@BT@ (or @BT@--ipc-host@BT@) to answer the LAN. A",
        "  wildcard bind is announced as @BT@127.0.0.1@BT@.",
        "- @BT@var/chassis.ipc@BT@ (mode @BT@0600@BT@) records @BT@pid@BT@,",
        "  @BT@host@BT@, @BT@port@BT@, @BT@token@BT@, @BT@started_at@BT@; clients read it, then",
        "  talk HTTP.",
        "- @BT@GET /health@BT@ is always open; @BT@GET /api/v1/status@BT@ and",
        "  @BT@POST /api/v1/shutdown@BT@ are gated by @BT@core.ipc.auth@BT@ and",
        "  @BT@core.ipc.allow_anonymous_shutdown@BT@. The header is",
        "  @BT@X-Korodex-Token@BT@.",
        f"- Query it with @BT@{project.script} status@BT@, or from any machine that has the",
        "  chassis installed: @BT@kdx status --target http://HOST:PORT --token SECRET@BT@.",
        "",
        "## Lifecycle and shutdown",
        "",
        "- Boot: discover paths, load config, build logging, create the bus, discover",
        "  modules, validate contracts, @BT@init()@BT@, @BT@register_cli()@BT@, @BT@start()@BT@,",
        "  then tick.",
        "- A module that raises in one phase is marked @BT@failed@BT@ and logged; the chassis",
        "  keeps booting, so one bad plugin never kills the process.",
        "- @BT@stop()@BT@ always runs, in reverse order, bounded by",
        "  @BT@core.lifecycle.stop_timeout@BT@.",
        "- Exit codes: SIGINT @BT@130@BT@, SIGTERM @BT@143@BT@, operator stop (including",
        "  @BT@POST /api/v1/shutdown@BT@) @BT@0@BT@. No @BT@KeyboardInterrupt@BT@",
        "  traceback, ever.",
        "",
        "## Events",
        "",
        "- One in-process bus per chassis, thread-safe, dot-segmented patterns (@BT@*@BT@",
        "  one segment, @BT@**@BT@ any).",
        "- Handlers run outside the bus lock and are timed, so a slow subscriber shows up",
        "  in bus statistics instead of hiding in a stall.",
        "- Modules never import each other: an event is the only sanctioned channel,",
        "  which is what keeps discovery order meaningful.",
        "",
        "## Extension points",
        "",
        f"1. **Add a module**: a directory under @BT@src/{package}/modules/@BT@ with",
        f"   @BT@module.py@BT@ and a manifest; @BT@{project.script} modules list@BT@ proves",
        "   discovery.",
        "2. **Add configuration**: a key under @BT@modules.<name>@BT@ in @BT@kdx.yml@BT@, read",
        "   with a typed getter.",
        "3. **Add a command**: @BT@register_cli@BT@ in the module; argparse and the wizard",
        "   update themselves.",
        "",
        "## Boundaries",
        "",
        "- Single process: no cluster, no scheduler, no persistence layer. Modules own",
        "  their storage; the chassis owns the directories under @BT@var/@BT@.",
        "- Modules run in-process; the contract and the stop timeout are the isolation.",
        "- Stdlib first: a dependency needs a paragraph of justification.",
        "",
        "See @BT@docs/AGENTS_GUIDE.md@BT@ for the contract in full and the verification",
        "  loop.",
    ]
    return "\n".join(lines).replace("@BT@", chr(96))


def _agents(project: ProjectTemplate) -> str:
    lines = [
        f"# AGENTS.md - {project.title}",
        "",
        "Working agreement for anyone editing this repository.",
        "",
        "## Commands",
        "",
        "@BT@@BT@@BT@bash",
        "make check      # ruff + mypy --strict + pytest",
        "make lint       # ruff check src tests",
        "make typecheck  # mypy",
        "make test       # pytest -q",
        f"{project.script} doctor   # environment, config provenance, modules",
        f"{project.script} status   # live daemon report",
        "@BT@@BT@@BT@",
        "",
        "## Layout",
        "",
        f"- @BT@src/{project.package}/modules/@BT@ - chassis modules, auto-discovered.",
        f"- @BT@src/{project.package}/cli.py@BT@ - the only entry point; keep it thin.",
        "- @BT@tests/@BT@ - pytest; no network, no sleeps longer than one tick.",
        "- @BT@kdx.yml@BT@ - configuration; never hardcode what belongs here.",
        "- @BT@var/@BT@ - runtime state (logs, IPC discovery file); never committed.",
        "",
        "## Rules",
        "",
        "1. **Headless first.** The chassis owns lifecycle; UIs are thin clients.",
        "2. **Zero stubs.** No TODO bodies, no @BT@pass@BT@ placeholders, no mock returns.",
        "3. **Precedence is sacred.** CLI > env > file > defaults; do not read",
        "   os.environ directly - the chassis already mapped it.",
        "4. **Clean shutdown.** Whatever @BT@start()@BT@ acquires, @BT@stop()@BT@ releases; SIGINT",
        "   exits 130, SIGTERM 143, an operator stop exits 0.",
        "5. **Diagnostics to stderr, UI to stdout.** Modules never print to stdout.",
        "6. **Crash-proof CLI.** Prompt in validation loops, defaults in brackets, and",
        "   never raise a stack trace at the operator.",
        "",
        "## Docs",
        "",
        "- @BT@docs/AGENTS_GUIDE.md@BT@ - module contract, manifest reference, and the",
        "  verification loop, written for AI agents and LLMs.",
        "- @BT@docs/ARCHITECTURE.md@BT@ - how this project is wired: boot order, config",
        "  layers, control plane, shutdown.",
        "",
        "## Skills",
        "",
        "@BT@.agents/skills/@BT@ holds the playbooks: project-setup, cli-wizard-ux, verification"
        + (", web-ui." if project.fullstack else "."),
        "",
    ]
    return "\n".join(lines).replace("@BT@", chr(96))


def _makefile(project: ProjectTemplate) -> str:
    python = "$(PYTHON)"
    return "\n".join(
        [
            "PYTHON ?= python3",
            "",
            ".PHONY: check lint format typecheck test run clean",
            "",
            "check: lint typecheck test",
            f'\t@echo "{project.distribution}: ruff, mypy, pytest all green"',
            "",
            "lint:",
            f"\t{python} -m ruff check src tests",
            "",
            "format:",
            f"\t{python} -m ruff format src tests",
            "",
            "typecheck:",
            f"\t{python} -m mypy",
            "",
            "test:",
            f"\t{python} -m pytest -q",
            "",
            "run:",
            f"\t{python} -m {project.package}.cli run",
            "",
            "clean:",
            "\trm -rf .pytest_cache .mypy_cache .ruff_cache dist build",
            "",
        ]
    )


def _gitignore() -> str:
    return "\n".join(
        [
            "__pycache__/",
            "*.py[cod]",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            ".coverage",
            "htmlcov/",
            "build/",
            "dist/",
            "*.egg-info/",
            ".venv/",
            "var/",
            "*.log",
            ".env",
            ".env.local",
            "kdx.local.yml",
            "korodex.local.yml",
            "node_modules/",
            "web/dist/",
            "",
        ]
    )


def _env_example(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            "# Env layer: copy to .env. KDX_CORE__FOO__BAR maps to the key core.foo.bar.",
            "# KORODEXIOS_* and the legacy KORODEX_* prefixes are also understood.",
            "KDX_CORE__LOG__LEVEL=INFO",
            "KDX_CORE__IPC__PORT=0",
            f"# KDX_MODULES__APP__GREETING=Hello from {project.title}",
            "",
        ]
    )


def _package_init(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            f'"""{project.title}: a korodexios-core chassis project."""',
            "",
            "from __future__ import annotations",
            "",
            "from typing import Final",
            "",
            f'PACKAGE: Final[str] = "{project.package}"',
            '"""Import name of this project."""',
            "",
            '__version__: Final[str] = "0.1.0"',
            '"""Project version, independent of the chassis version."""',
            "",
            '__all__ = ["PACKAGE", "__version__"]',
            "",
        ]
    )


def _cli_source(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            '"""Command line entry point: a thin client over the chassis CLI."""',
            "",
            "from __future__ import annotations",
            "",
            "import sys",
            "from collections.abc import Sequence",
            "from pathlib import Path",
            "",
            "from korodexios_core.cli.entry import main as chassis_main",
            "",
            "",
            "def project_root() -> Path:",
            '    """Return the repository root that owns kdx.yml."""',
            "    here = Path(__file__).resolve()",
            "    for candidate in (*here.parents[2:3], Path.cwd()):",
            '        if (candidate / "kdx.yml").is_file():',
            "            return candidate",
            "    return Path.cwd()",
            "",
            "",
            "def main(argv: Sequence[str] | None = None) -> int:",
            '    """Boot the chassis against this project root and dispatch the CLI."""',
            "    args = list(sys.argv[1:] if argv is None else argv)",
            '    if "--home" not in args:',
            '        args[:0] = ["--home", str(project_root())]',
            "    return chassis_main(args)",
            "",
            "",
            'if __name__ == "__main__":  # pragma: no cover - manual invocation',
            "    sys.exit(main())",
            "",
        ]
    )


def _app_package(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            '"""The app module: a plugin root entry discovered by the chassis."""',
            "",
            "from __future__ import annotations",
            "",
            "from .module import MODULE, AppModule",
            "",
            '__all__ = ["MODULE", "AppModule"]',
            "",
        ]
    )


def _app_manifest(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            "[module]",
            'name = "app"',
            f'description = "Project module generated by korodex init for {project.title}."',
            'entrypoint = "module.MODULE"',
            'version = "0.1.0"',
            "priority = 100",
            'requires_core = ">=0.1,<1.0"',
            "depends_on = []",
            "",
        ]
    )


def _app_module(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            '"""The app module: the smallest complete chassis module."""',
            "",
            "from __future__ import annotations",
            "",
            "from typing import Any",
            "",
            "from korodexios_core.cli.render import dump_json, write_status",
            "from korodexios_core.constants import TAG_NOTE, TAG_OK",
            "from korodexios_core.kernel.cli_contract import (",
            "    CliArgument,",
            "    CliCommand,",
            "    CliRequest,",
            "    CliSurface,",
            ")",
            "from korodexios_core.kernel.context import CoreContext",
            "from korodexios_core.kernel.contract import Module",
            "",
            'GREETING_EVENT = "app.greeted"',
            '"""Event published when the module starts."""',
            "",
            "",
            "class AppModule(Module):",
            '    """Greets on demand and counts how often it did."""',
            "",
            '    name = "app"',
            f'    description = "Project module generated by korodex init for {project.title}."',
            '    version = "0.1.0"',
            "",
            "    greetings = 0",
            "",
            "    def init(self, context: CoreContext) -> None:",
            '        """Read project settings so the greeting stays configurable."""',
            "        super().init(context)",
            "        self.greetings = 0",
            f'        default = "Hello from {project.title}"',
            '        self.greeting = self.config.get_str("greeting", default)',
            "",
            "    def register_cli(self, surface: CliSurface) -> None:",
            '        """Add a "hello" command to the project command tree."""',
            "        surface.add_command(",
            "            CliCommand(",
            '                name="hello",',
            '                help="Say the configured greeting and count it.",',
            "                args=(",
            "                    CliArgument(",
            '                        ("--json",),',
            '                        dest="as_json",',
            '                        action="store_true",',
            '                        help="Emit the greeting as JSON.",',
            "                    ),",
            "                ),",
            "                handler=self._hello,",
            "                module_name=self.name,",
            "            )",
            "        )",
            "",
            "    def start(self) -> None:",
            '        """Publish readiness so other modules can react."""',
            '        payload = {"greeting": self.greeting}',
            "        self.bus.publish(GREETING_EVENT, payload, source=self.name)",
            "",
            "    def stop(self) -> None:",
            '        """Nothing to release: the module holds no resources."""',
            '        self.logger.debug("app module stopped", greetings=self.greetings)',
            "",
            "    def health(self) -> dict[str, Any]:",
            '        """Return counters the chassis can surface."""',
            '        return {"greetings": self.greetings, "greeting": self.greeting}',
            "",
            "    def _hello(self, request: CliRequest) -> int:",
            '        """Handle the "hello" command."""',
            "        self.greetings += 1",
            "        payload = {",
            '            "module": self.name,',
            '            "greeting": self.greeting,',
            '            "greetings": self.greetings,',
            "        }",
            '        if request.flag("as_json"):',
            "            dump_json(payload)",
            "        else:",
            "            write_status(",
            '                f"{self.greeting}  (greetings={self.greetings})",',
            "                tag=TAG_OK if self.greetings == 1 else TAG_NOTE,",
            "            )",
            "        return 0",
            "",
            "",
            "MODULE = AppModule()  # the instance handed to discovery",
            "",
        ]
    )


def _smoke_test(project: ProjectTemplate) -> str:
    package = project.package
    title = (
        '"""Smoke test: the chassis boots this project and finds a bare module root."""'
        if project.minimal
        else '"""Smoke test: discovery, configuration, and the module contract hold."""'
    )
    third_party: list[str] = [] if project.minimal else ["import yaml"]
    chassis_imports = [
        "from korodexios_core.kernel.chassis import Chassis, ChassisOptions, ChassisState"
    ]
    if not project.minimal:
        chassis_imports.append(
            "from korodexios_core.kernel.contract import ModuleState, check_contract"
        )
    chassis_imports.append("from korodexios_core.paths import AppPaths")
    first_party = [f"from {package} import __version__", f"from {package}.cli import project_root"]
    if not project.minimal:
        first_party.append(f"from {package}.modules.app.module import MODULE")
    tests = [
        "",
        "",
        "def test_project_root_holds_configuration() -> None:",
        '    """The entry point must find kdx.yml next to the source tree."""',
        "    root = project_root()",
        '    assert (root / "kdx.yml").is_file()',
        '    assert (root / "pyproject.toml").is_file()',
        "",
        "",
        "def test_version_is_exposed() -> None:",
        '    """The package exposes a version string."""',
        '    assert __version__.count(".") == 2',
        "",
        "",
        "def _boot() -> Chassis:",
        '    """Boot the chassis against this project root."""',
        "    paths = AppPaths.discover(root=str(project_root()), environ={})",
        "    return Chassis.create(",
        "        ChassisOptions(",
        "            home=paths.root,",
        "            environ={},",
        '            cli_overrides={"core.log.file_enabled": False, "core.ipc.enabled": False},',
        "            install_signals=False,",
        "        )",
        "    )",
        "",
        "",
    ]
    if project.minimal:
        tests += [
            "def test_chassis_boots_a_bare_project() -> None:",
            '    """The chassis starts, skips the example modules, and stops cleanly."""',
            "    chassis = _boot()",
            "    chassis.initialize()",
            "    chassis.start(ipc=False)",
            "    try:",
            "        assert chassis.state is ChassisState.RUNNING",
            "        names = chassis.registry.names()",
            '        assert "heartbeat" not in names',
            '        assert "backup_engine" not in names',
            '        assert "app" not in names',
            "    finally:",
            "        assert int(chassis.shutdown()) == 0",
            "",
        ]
    else:
        tests += [
            "def test_app_module_satisfies_the_contract() -> None:",
            '    """The generated module is a real chassis module."""',
            "    assert check_contract(MODULE) == []",
            '    assert MODULE.name == "app"',
            "    assert MODULE.state is ModuleState.NEW",
            "",
            "",
            "def test_chassis_discovers_the_app_module() -> None:",
            '    """Discovery finds the project module and stops it cleanly."""',
            "    chassis = _boot()",
            "    chassis.initialize()",
            "    chassis.start(ipc=False)",
            "    try:",
            "        assert chassis.state is ChassisState.RUNNING",
            '        assert "app" in chassis.registry.names()',
            '        assert chassis.registry.health()["failed"] == []',
            "    finally:",
            "        assert int(chassis.shutdown()) == 0",
            "    health = chassis.registry.health()",
            '    states = {item["name"]: item["state"] for item in health["modules"]}',
            '    assert states["app"] == "stopped"',
            "",
            "",
            "def test_configuration_declares_the_module_root() -> None:",
            '    """kdx.yml points discovery at the project modules."""',
            "    data = yaml.safe_load(",
            '        (project_root() / "kdx.yml").read_text(encoding="utf-8")',
            "    )",
            "    assert data is not None",
            '    extra = data["core"]["modules"]["extra_paths"]',
            f'    assert extra == ["src/{package}/modules"]',
            "",
        ]
    head = [title, "", "from __future__ import annotations", ""]
    return "\n".join([*head, *third_party, *chassis_imports, "", *first_party, *tests])


def _web_package(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            "{",
            f'  "name": "{project.distribution}-web",',
            '  "private": true,',
            '  "version": "0.1.0",',
            '  "type": "module",',
            '  "scripts": {',
            '    "dev": "python3 -m http.server 5173 --directory .",',
            '    "serve": "python3 -m http.server 5173 --directory ."',
            "  }",
            "}",
            "",
        ]
    )


def _web_index(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "  <head>",
            '    <meta charset="utf-8" />',
            f"    <title>{project.title} - chassis status</title>",
            '    <link rel="stylesheet" href="./src/styles.css" />',
            "  </head>",
            "  <body>",
            f"    <h1>{project.title}</h1>",
            '    <p class="hint">',
            "      The daemon binds 127.0.0.1 and records its port and token in",
            "      <code>var/chassis.ipc</code>. Pass them to this page as",
            "      <code>?port=8123&amp;token=SECRET</code>.",
            "    </p>",
            '    <pre id="report">loading...</pre>',
            '    <script type="module" src="./src/main.js"></script>',
            "  </body>",
            "</html>",
            "",
        ]
    )


def _web_main(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            "// Talks to the chassis control plane: no build step, no framework.",
            "const params = new URLSearchParams(window.location.search);",
            'const host = params.get("host") || "127.0.0.1";',
            'const port = params.get("port") || "8123";',
            'const token = params.get("token") || "";',
            'const target = "http://" + host + ":" + port + "/api/v1/status";',
            'const report = document.querySelector("#report");',
            "",
            "async function load() {",
            '  const response = await fetch(target, { headers: { "X-Korodex-Token": token } });',
            "  if (!response.ok) {",
            '    report.textContent = "chassis answered HTTP " + response.status;',
            "    return;",
            "  }",
            "  const data = await response.json();",
            "  const uptime = Math.round(data.app.uptime_seconds);",
            "  report.textContent =",
            '    data.app.state + " - " + data.modules.total + " modules, up " + uptime + "s";',
            "}",
            "",
            "load().catch((error) => {",
            '  report.textContent = "chassis unreachable: " + error.message;',
            "});",
            "",
        ]
    )


def _web_styles(project: ProjectTemplate) -> str:
    return "\n".join(
        [
            "/* Minimal chrome for the chassis status page. */",
            ":root {",
            "  color-scheme: light dark;",
            "  font-family: ui-sans-serif, system-ui, sans-serif;",
            "}",
            "body {",
            "  margin: 2rem auto;",
            "  max-width: 46rem;",
            "  padding: 0 1rem;",
            "}",
            "h1 {",
            "  font-size: 1.4rem;",
            "  margin-bottom: 0.25rem;",
            "}",
            ".hint {",
            "  color: color-mix(in srgb, currentColor 65%, transparent);",
            "}",
            "#report {",
            "  border: 1px solid color-mix(in srgb, currentColor 20%, transparent);",
            "  border-radius: 0.5rem;",
            "  padding: 1rem;",
            "  white-space: pre-wrap;",
            "}",
            "",
        ]
    )


def _skill(name: str, description: str, sections: list[str]) -> str:
    return "\n".join([f"# Skill: {name}", "", description, "", *sections, ""])


def _skill_project_setup(project: ProjectTemplate) -> str:
    return _skill(
        "project-setup",
        f"Keep {project.title} a clean chassis project.",
        [
            "## When to use",
            "Adding a dependency, editing pyproject.toml, or adding a directory.",
            "",
            "## Steps",
            f"1. Code lives in @BT@src/{project.package}/@BT@, never at the root.",
            "2. Module roots go in @BT@kdx.yml@BT@ under @BT@core.modules.extra_paths@BT@.",
            "3. Runtime state lives in @BT@var/@BT@ (gitignored); never write next to sources.",
            "4. Runtime deps go in @BT@[project].dependencies@BT@; tooling in @BT@dev@BT@.",
            "5. @BT@pip install -e ../korodexios-core@BT@ (or a git URL) provides the chassis.",
            "",
            "## Verification",
            "@BT@make check@BT@ after every change; @BT@kdx doctor@BT@ for config answers.",
        ],
    ).replace("@BT@", chr(96))


def _skill_cli_wizard(project: ProjectTemplate) -> str:
    return _skill(
        "cli-wizard-ux",
        "Make every prompt crash-proof, per the chassis rulebook.",
        [
            "## When to use",
            "Adding or editing a command, prompt, or menu in this project.",
            "",
            "## Rules",
            "- Numbered menus: @BT@[1]@BT@, @BT@[2]@BT@; ENTER accepts the bracketed default.",
            "- Every free-text input runs in a @BT@while True@BT@ validation loop.",
            "- Invalid input: polite @BT@[!]@BT@ message, then re-prompt - never a stack trace.",
            "- Mandatory @BT@--yes@BT@ plus non-TTY detection: CI must never block on a prompt.",
            "- Tags: @BT@[✓]@BT@ ok, @BT@[?]@BT@ question, @BT@[!]@BT@ problem, @BT@[i]@BT@ note.",
            f"- Reuse @BT@cli.ux.UserExperience@BT@ from the chassis; {project.script} stays thin.",
            "",
            "## Verification",
            "@BT@echo | korodex wizard@BT@ and @BT@korodex --yes status@BT@ never block.",
        ],
    ).replace("@BT@", chr(96))


def _skill_verification(project: ProjectTemplate) -> str:
    return _skill(
        "verification",
        "Prove a change instead of assuming it.",
        [
            "## Loop",
            "1. @BT@make lint@BT@ - ruff over @BT@src@BT@ and @BT@tests@BT@.",
            "2. @BT@make typecheck@BT@ - mypy strict over the package.",
            "3. @BT@make test@BT@ - pytest; no network, no sleeps longer than one tick.",
            "4. Boot it: @BT@kdx run --once@BT@, then @BT@kdx status@BT@, then Ctrl-C and",
            "   check the exit code (130 SIGINT, 143 SIGTERM, 0 for an operator stop).",
            "",
            "## Evidence",
            "Quote the failing command and its output before and after the fix. A green",
            "suite that never touches the changed path is not proof for that path.",
        ],
    ).replace("@BT@", chr(96))


def _skill_web_ui(project: ProjectTemplate) -> str:
    return _skill(
        "web-ui",
        f"Keep the {project.title} web client honest about the daemon.",
        [
            "## Contract",
            "- The daemon binds 127.0.0.1 only; the token lives in @BT@var/chassis.ipc@BT@.",
            "- @BT@/health@BT@ is open; the other endpoints need the token.",
            "- The token never ships in committed HTML or JS.",
            "",
            "## Steps",
            "1. @BT@kdx run@BT@ in one terminal, @BT@npm --prefix web run dev@BT@ in another.",
            "2. Read the port and token from @BT@var/chassis.ipc@BT@ for the dev proxy.",
            "3. Keep @BT@web/@BT@ free of build output; @BT@web/dist/@BT@ is gitignored.",
        ],
    ).replace("@BT@", chr(96))


__all__ = [
    "DEFAULT_AUTHOR",
    "ECOSYSTEMS",
    "ECOSYSTEM_LABELS",
    "EXAMPLE_MODULES",
    "ProjectError",
    "ProjectResult",
    "ProjectTemplate",
    "build_files",
    "ecosystem_key",
    "init_project",
    "project_from_inputs",
    "prompt_project",
    "validate_project_name",
]
