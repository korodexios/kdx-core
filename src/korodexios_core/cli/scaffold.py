"""Scaffolding for new modules: one command, one directory, no placeholders."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODULE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
PACKAGE_MODULES_DIR = "korodexios_core/modules"


class ScaffoldError(ValueError):
    """Raised when a module directory cannot be scaffolded safely."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(slots=True)
class ScaffoldResult:
    """Where a scaffolded module lives and which files were written."""

    directory: Path
    created: list[Path] = field(default_factory=list)
    replaced: bool = False
    test_file: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for the CLI."""
        return {
            "directory": str(self.directory),
            "created": [str(item) for item in self.created],
            "replaced": self.replaced,
            "test_file": str(self.test_file) if self.test_file else None,
        }


def pascal_case(name: str) -> str:
    """Return the class-name spelling for a module name (backup_engine -> BackupEngine)."""
    parts = [part for part in re.split(r"[^a-z0-9]+", str(name).lower()) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Plugin"


def validate_module_name(name: str) -> str:
    """Return the cleaned name or raise ScaffoldError with a friendly hint."""
    cleaned = str(name).strip()
    if not cleaned:
        raise ScaffoldError(
            "a module name is required",
            hint="Use lower_case with digits or underscores, for example 'backup_engine'.",
        )
    if not MODULE_NAME_PATTERN.match(cleaned):
        raise ScaffoldError(
            f"{cleaned!r} is not a valid module name",
            hint="Use lower_case with digits or underscores, for example 'backup_engine'.",
        )
    return cleaned


def scaffold_module(
    parent: Path,
    name: str,
    *,
    title: str = "",
    force: bool = False,
    project_root: Path | None = None,
) -> ScaffoldResult:
    """Create a working module package in *parent*/*name* and return what was written."""
    cleaned = validate_module_name(name)
    parent = Path(parent)
    directory = parent / cleaned
    if not parent.exists():
        try:
            parent.mkdir(parents=True)
        except OSError as exc:
            raise ScaffoldError(
                f"cannot create {parent}: {exc.strerror or exc}",
                hint="Pick a writable directory with --path.",
            ) from exc
    existed = directory.exists() and any(directory.iterdir())
    if existed and not force:
        raise ScaffoldError(
            f"{directory} already holds a module",
            hint="Pass --force to replace the files, or choose another name.",
        )
    directory.mkdir(parents=True, exist_ok=True)
    class_name = pascal_case(cleaned)
    description = title.strip() or f"{cleaned} module for the korodexios chassis."
    result = ScaffoldResult(directory=directory, replaced=existed)
    files = {
        "kdx_module.toml": _manifest(cleaned, description),
        "module.py": _module_source(cleaned, class_name, description),
        "__init__.py": _package_source(cleaned, class_name, description),
    }
    for filename, content in files.items():
        target = directory / filename
        target.write_text(content, encoding="utf-8")
        result.created.append(target)
    test_file = _write_test(parent, cleaned, project_root, description)
    if test_file is not None:
        result.test_file = test_file
    return result


def _manifest(name: str, description: str) -> str:
    return (
        "[module]\n"
        f'name = "{name}"\n'
        f'description = "{description}"\n'
        'version = "0.1.0"\n'
        'entrypoint = "module.MODULE"\n'
        "priority = 100\n"
        'requires_core = ">=0.1,<1.0"\n'
        "depends_on = []\n"
    )


def _package_source(name: str, class_name: str, description: str) -> str:
    exported = f"{class_name}Module"
    return (
        f'"""{name}: {description}"""\n\n'
        f"from .module import MODULE, {exported}\n\n"
        f'__all__ = ["MODULE", "{exported}"]\n'
        f'\n\n__version__ = "0.1.0"\n'
    )


def _module_source(name: str, class_name: str, description: str) -> str:
    command = name.split("_")[0]
    return (
        f'"""{name} module: {description}"""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
        "from korodexios_core.cli.render import dump_json, write_status\n"
        "from korodexios_core.kernel.cli_contract import CliArgument, CliCommand\n"
        "from korodexios_core.kernel.contract import Module\n\n\n"
        f"class {class_name}Module(Module):\n"
        f'    """{description}"""\n\n'
        f'    name = "{name}"\n'
        f'    description = "{description}"\n'
        '    version = "0.1.0"\n\n'
        "    reports = 0\n\n"
        "    def init(self, context: Any) -> None:\n"
        "        super().init(context)\n"
        "        self.reports = 0\n\n"
        "    def register_cli(self, surface: Any) -> None:\n"
        "        surface.add_command(\n"
        "            CliCommand(\n"
        f'                name="{command}",\n'
        f'                help="{name} commands.",\n'
        "                subcommands=(\n"
        "                    CliCommand(\n"
        '                        name="ping",\n'
        f'                        help="Confirm {name} is wired into the chassis.",\n'
        "                        args=(\n"
        "                            CliArgument(\n"
        '                                ("--json",),\n'
        '                                dest="as_json",\n'
        '                                action="store_true",\n'
        '                                help="Emit the report as JSON.",\n'
        "                            ),\n"
        "                        ),\n"
        "                        handler=self._ping,\n"
        "                        module_name=self.name,\n"
        "                    ),\n"
        "                ),\n"
        "            )\n"
        "        )\n\n"
        "    def start(self) -> None:\n"
        f'        self.logger.info("[✓] {name} ready")\n\n'
        "    def stop(self) -> None:\n"
        f'        self.logger.info("[i] {name} released")\n\n'
        "    def health(self) -> dict[str, Any]:\n"
        '        """Return counters the chassis can surface."""\n'
        '        return {"reports": self.reports}\n\n'
        "    def _ping(self, request: Any) -> int:\n"
        "        self.reports += 1\n"
        "        payload = {\n"
        '            "module": self.name,\n'
        '            "reports": self.reports,\n'
        '            "alive": True,\n'
        "        }\n"
        '        if request.flag("as_json"):\n'
        "            dump_json(payload)\n"
        "            return 0\n"
        '        write_status(f"{self.name} answered {self.reports}")\n'
        "        return 0\n\n\n"
        f"MODULE = {class_name}Module()\n"
    )


def _write_test(
    parent: Path,
    name: str,
    project_root: Path | None,
    description: str,
) -> Path | None:
    """Add a real smoke test when the module lives inside the chassis package."""
    root = Path(project_root) if project_root is not None else None
    if root is None:
        return None
    inside = (root / PACKAGE_MODULES_DIR).resolve()
    try:
        parent.resolve().relative_to(inside)
    except ValueError:
        return None
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return None
    target = tests_dir / f"test_{name}.py"
    content = (
        f'"""Smoke tests for the {name} module."""\n\n'
        "from __future__ import annotations\n\n"
        "from korodexios_core.kernel.contract import check_contract\n"
        f"from korodexios_core.modules.{name}.module import MODULE\n\n\n"
        "def test_module_satisfies_the_contract() -> None:\n"
        f'    assert "{name}" == MODULE.name\n'
        "    assert check_contract(MODULE) == []\n\n\n"
        "def test_module_starts_clean() -> None:\n"
        '    assert MODULE.health()["reports"] == 0\n'
    )
    target.write_text(content, encoding="utf-8")
    return target


__all__ = [
    "MODULE_NAME_PATTERN",
    "ScaffoldError",
    "ScaffoldResult",
    "pascal_case",
    "scaffold_module",
    "validate_module_name",
]
