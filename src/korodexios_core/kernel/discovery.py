"""Auto-discovery: scan the built-in modules directory plus external plugin dirs."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import re
import sys
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import MANIFEST_NAME, MODULE_PACKAGE
from ..exceptions import ModuleContractError, ModuleDiscoveryError
from ..paths import AppPaths
from ..version import __version__, parse_version
from .contract import ModuleSpec, check_contract

_REQUIREMENT_PATTERN = re.compile(r"^\s*(==|>=|<=|!=|>|<)?\s*([0-9][A-Za-z0-9._+-]*)\s*$")
_SKIP_PREFIXES = ("_", ".")


@contextlib.contextmanager
def _temporary_sys_path(directory: Path) -> Iterator[str]:
    """Expose *directory* on sys.path so a plugin can import sibling helpers.

    A missing directory is skipped, so a project without a ``src`` layout simply
    adds nothing to the import path.
    """
    entry = str(directory)
    added = entry not in sys.path and directory.is_dir()
    if added:
        sys.path.insert(0, entry)
    try:
        yield entry
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(entry)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", str(text).lower()).strip("_")
    return cleaned or "ext"


def _docstring_line(obj: Any) -> str:
    doc = inspect.getdoc(obj) or ""
    for line in doc.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _manifest_table(data: Mapping[str, Any]) -> dict[str, Any]:
    table = data.get("module")
    return dict(table) if isinstance(table, Mapping) else {}


def _manifest_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    extra = data.get("metadata")
    if isinstance(extra, Mapping):
        metadata.update({str(key): value for key, value in extra.items()})
    for key, value in data.items():
        if key not in ("module", "metadata"):
            metadata.setdefault(str(key), value)
    return metadata


def _version_satisfies(version: str, requirement: str) -> bool:
    """Evaluate a comma-separated specifier set such as '>=0.1,<1.0'."""
    try:
        left = parse_version(version).as_tuple()
    except ValueError:
        return False
    for clause in str(requirement).split(","):
        cleaned = clause.strip()
        if not cleaned:
            continue
        match = _REQUIREMENT_PATTERN.match(cleaned)
        if match is None:
            return False
        operator = match.group(1) or "=="
        try:
            right = parse_version(match.group(2)).as_tuple()
        except ValueError:
            return False
        comparisons = {
            "==": left == right,
            "!=": left != right,
            ">=": left >= right,
            "<=": left <= right,
            ">": left > right,
            "<": left < right,
        }
        if not comparisons[operator]:
            return False
    return True


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    """A module that could not be loaded, with the stage that rejected it."""

    name: str
    stage: str
    message: str
    origin: str = ""

    def describe(self) -> str:
        """Return the one-line rendering used by 'kdx modules list'."""
        where = f" ({self.origin})" if self.origin else ""
        return f"{self.name} [{self.stage}]{where}: {self.message}"


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """A module instance plus the spec that produced it."""

    spec: ModuleSpec
    instance: Any


@dataclass(slots=True)
class DiscoveryReport:
    """Outcome of one discovery sweep."""

    candidates: list[DiscoveryCandidate] = field(default_factory=list)
    failures: list[DiscoveryFailure] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def names(self) -> list[str]:
        """Return the names of every loadable module."""
        return [candidate.spec.name for candidate in self.candidates]

    def summary(self) -> str:
        """Return the counts line printed after a scan."""
        return (
            f"ready={len(self.candidates)} skipped={len(self.skipped)} failed={len(self.failures)}"
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the sweep."""
        return {
            "ready": [candidate.spec.as_dict() for candidate in self.candidates],
            "skipped": [{"name": name, "reason": reason} for name, reason in self.skipped],
            "failures": [
                {
                    "name": failure.name,
                    "stage": failure.stage,
                    "message": failure.message,
                    "origin": failure.origin,
                }
                for failure in self.failures
            ],
        }


class ModuleDiscovery:
    """Filesystem scanner turning directories into validated module candidates."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        config: Any = None,
        enabled: Any = None,
        disabled: Any = None,
        autodiscover: bool = True,
        logger: Any = None,
    ) -> None:
        self._paths = paths
        self._config = config
        self._allowlist = {str(name) for name in (enabled or [])}
        self._blocklist = {str(name) for name in (disabled or [])}
        self._autodiscover = bool(autodiscover)
        self._logger = logger

    def scan(self) -> DiscoveryReport:
        """Scan built-in and external directories, isolating every failure."""
        report = DiscoveryReport()
        if not self._autodiscover:
            report.skipped.append(("*", "core.modules.autodiscover is false"))
            self._log("info", "module auto-discovery disabled")
            return report

        builtin_dir = self._paths.builtin_modules_dir
        if builtin_dir.is_dir():
            self._scan_directory(builtin_dir, "builtin", report, package_prefix=MODULE_PACKAGE)
        else:
            report.failures.append(
                DiscoveryFailure(
                    "modules",
                    "scan",
                    f"built-in modules directory {builtin_dir} is missing",
                    str(builtin_dir),
                )
            )

        for directory in self._paths.extra_module_dirs:
            if not directory.is_dir():
                report.failures.append(
                    DiscoveryFailure(
                        directory.name or str(directory),
                        "scan",
                        f"plugin directory {directory} is missing",
                        str(directory),
                    )
                )
                continue
            self._scan_directory(directory, "external", report, package_prefix=None)

        report.candidates.sort(key=lambda item: (item.spec.priority, item.spec.name))
        self._log(
            "debug",
            "module discovery sweep finished",
            ready=len(report.candidates),
            skipped=len(report.skipped),
            failed=len(report.failures),
        )
        return report

    def _scan_directory(
        self,
        directory: Path,
        origin: str,
        report: DiscoveryReport,
        *,
        package_prefix: str | None,
    ) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if child.name.startswith(_SKIP_PREFIXES) or child.name == "__pycache__":
                continue
            if child.is_dir():
                init = child / "__init__.py"
                if not init.is_file():
                    report.skipped.append((child.name, "package without __init__.py"))
                    continue
                manifest_path = child / MANIFEST_NAME
                self._load_candidate(
                    name=child.name,
                    package_dir=child,
                    entry_file=init,
                    manifest_path=manifest_path if manifest_path.is_file() else None,
                    origin=origin,
                    package_prefix=package_prefix,
                    report=report,
                )
            elif child.is_file() and child.suffix == ".py" and child.stem != "__init__":
                self._load_candidate(
                    name=child.stem,
                    package_dir=None,
                    entry_file=child,
                    manifest_path=None,
                    origin=origin,
                    package_prefix=package_prefix,
                    report=report,
                )

    def _load_candidate(
        self,
        name: str,
        *,
        package_dir: Path | None,
        entry_file: Path,
        manifest_path: Path | None,
        origin: str,
        package_prefix: str | None,
        report: DiscoveryReport,
    ) -> None:
        manifest = self._read_manifest(manifest_path, name, report) if manifest_path else {}
        entrypoint = str(_manifest_table(manifest).get("entrypoint") or "MODULE")
        try:
            module_obj = self._import(name, package_dir, entry_file, origin, package_prefix)
        except ModuleDiscoveryError as exc:
            report.failures.append(DiscoveryFailure(name, exc.stage, exc.message, str(entry_file)))
            return
        except Exception as exc:
            report.failures.append(
                DiscoveryFailure(
                    name,
                    "import",
                    f"{type(exc).__name__}: {exc}",
                    str(entry_file),
                )
            )
            return

        try:
            resolved = self._resolve_entrypoint(module_obj, entrypoint, name)
            instance = self._instantiate(resolved, name)
            spec = self._build_spec(name, manifest, module_obj, instance, origin, entry_file)
            spec.validate()
        except ModuleDiscoveryError as exc:
            report.failures.append(DiscoveryFailure(name, exc.stage, exc.message, str(entry_file)))
            return
        except ModuleContractError as exc:
            report.failures.append(DiscoveryFailure(name, "contract", exc.message, str(entry_file)))
            return

        violations = check_contract(instance)
        if violations:
            report.failures.append(
                DiscoveryFailure(name, "contract", "; ".join(violations), str(entry_file))
            )
            return
        if not spec.enabled or not self._is_enabled(spec):
            report.skipped.append((spec.name, "disabled by manifest or configuration"))
            return
        if spec.requires_core and not _version_satisfies(__version__, spec.requires_core):
            report.failures.append(
                DiscoveryFailure(
                    spec.name,
                    "requires_core",
                    f"needs core {spec.requires_core}, chassis is {__version__}",
                    str(entry_file),
                )
            )
            return
        report.candidates.append(DiscoveryCandidate(spec=spec, instance=instance))

    def _read_manifest(self, path: Path, name: str, report: DiscoveryReport) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError, EOFError) as exc:
            report.failures.append(
                DiscoveryFailure(name, "manifest", f"{MANIFEST_NAME}: {exc}", str(path))
            )
            return {}
        return dict(data)

    def _import(
        self,
        name: str,
        package_dir: Path | None,
        entry_file: Path,
        origin: str,
        package_prefix: str | None,
    ) -> Any:
        if package_prefix:
            dotted = (
                f"{package_prefix}.{name}" if package_dir else f"{package_prefix}.{entry_file.stem}"
            )
            try:
                return importlib.import_module(dotted)
            except ImportError as exc:
                raise ModuleDiscoveryError(
                    f"import of {dotted} failed: {exc}",
                    module_name=name,
                    stage="import",
                    hint="Check the module package for syntax errors and missing dependencies.",
                ) from exc

        holder = package_dir.parent.name if package_dir else entry_file.parent.name
        module_name = f"korodexios_ext_{_slug(holder)}_{_slug(name)}"
        if package_dir is not None:
            spec = importlib.util.spec_from_file_location(
                module_name,
                package_dir / "__init__.py",
                submodule_search_locations=[str(package_dir)],
            )
        else:
            spec = importlib.util.spec_from_file_location(module_name, entry_file)
        if spec is None or spec.loader is None:
            raise ModuleDiscoveryError(
                f"cannot build an import spec for {entry_file}",
                module_name=name,
                stage="import",
            )
        module_obj = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module_obj
        sibling_dir = package_dir if package_dir is not None else entry_file.parent
        # A module may import its own project package: expose <root>/src while
        # the module is executed, then take it away again.
        project_src = self._paths.root / "src"
        with _temporary_sys_path(sibling_dir), _temporary_sys_path(project_src):
            try:
                spec.loader.exec_module(module_obj)
            except Exception as exc:
                sys.modules.pop(module_name, None)
                raise ModuleDiscoveryError(
                    f"import of {module_name} failed: {type(exc).__name__}: {exc}",
                    module_name=name,
                    stage="import",
                    hint="Check the plugin for syntax errors and missing dependencies.",
                ) from exc
        return module_obj

    def _resolve_entrypoint(self, module_obj: Any, entrypoint: str, name: str) -> Any:
        parts = [part for part in str(entrypoint).split(".") if part]
        if not parts:
            raise ModuleDiscoveryError(
                f"empty entrypoint {entrypoint!r}", module_name=name, stage="entrypoint"
            )
        cursor: Any = module_obj
        for part in parts:
            nxt = getattr(cursor, part, None)
            if nxt is None:
                base = getattr(cursor, "__name__", None)
                if isinstance(base, str):
                    try:
                        nxt = importlib.import_module(f"{base}.{part}")
                    except ImportError:
                        nxt = None
                    else:
                        with contextlib.suppress(AttributeError, TypeError):
                            setattr(cursor, part, nxt)
            if nxt is None:
                location = getattr(module_obj, "__name__", module_obj)
                raise ModuleDiscoveryError(
                    f"entrypoint {entrypoint!r} not found in {location}",
                    module_name=name,
                    stage="entrypoint",
                    hint="Expose a module instance named MODULE, or set the manifest entrypoint.",
                )
            cursor = nxt
        return cursor

    def _instantiate(self, resolved: Any, name: str) -> Any:
        if inspect.isclass(resolved):
            try:
                return resolved()
            except TypeError as exc:
                raise ModuleDiscoveryError(
                    f"class {resolved.__name__} cannot be constructed: {exc}",
                    module_name=name,
                    stage="instantiate",
                    hint="Constructor arguments are not supported; use a factory function.",
                ) from exc
        if callable(resolved) and not _looks_like_module(resolved):
            try:
                return resolved()
            except Exception as exc:
                raise ModuleDiscoveryError(
                    f"factory failed: {type(exc).__name__}: {exc}",
                    module_name=name,
                    stage="instantiate",
                ) from exc
        return resolved

    def _build_spec(
        self,
        name: str,
        manifest: Mapping[str, Any],
        module_obj: Any,
        instance: Any,
        origin: str,
        source: Path,
    ) -> ModuleSpec:
        table = _manifest_table(manifest)
        description = str(
            table.get("description")
            or getattr(instance, "description", "")
            or _docstring_line(module_obj)
        )
        version = str(
            table.get("version")
            or getattr(instance, "version", "")
            or getattr(module_obj, "__version__", "")
            or "0.0.0"
        )
        entrypoint = str(table.get("entrypoint") or "MODULE")
        requires = table.get("requires_core")
        priority_value = table.get("priority", 100)
        try:
            priority = int(priority_value)
        except (TypeError, ValueError) as exc:
            raise ModuleDiscoveryError(
                f"manifest priority must be a whole number, got {priority_value!r}",
                module_name=name,
                stage="manifest",
            ) from exc
        declared = table.get("depends_on")
        if declared is None:
            declared = getattr(instance, "depends_on", ())
        if isinstance(declared, str):
            declared = [declared]
        if not isinstance(declared, (list, tuple)):
            raise ModuleDiscoveryError(
                f"manifest depends_on must be a list of module names, got {declared!r}",
                module_name=name,
                stage="manifest",
            )
        dependencies = tuple(str(item).strip() for item in declared if str(item).strip())
        return ModuleSpec(
            name=str(table.get("name") or name),
            description=description,
            version=version,
            entrypoint=entrypoint,
            enabled=bool(table.get("enabled", True)),
            priority=priority,
            depends_on=dependencies,
            origin=origin,
            source=source,
            requires_core=str(requires) if requires else None,
            metadata=_manifest_metadata(manifest),
        )

    def _is_enabled(self, spec: ModuleSpec) -> bool:
        if self._allowlist and spec.name not in self._allowlist:
            return False
        if spec.name in self._blocklist:
            return False
        if self._config is None:
            return True
        try:
            return bool(self._config.get_bool(f"modules.{spec.name}.enabled", True))
        except Exception:
            return True

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self._logger is None:
            return
        emitter = getattr(self._logger, level, None)
        if callable(emitter):
            emitter(message, **fields)


def _looks_like_module(candidate: Any) -> bool:
    members = ("name", "description", "version", "init", "register_cli", "start", "stop")
    return all(hasattr(candidate, member) for member in members)


def version_satisfies(version: str, requirement: str) -> bool:
    """Public helper used by tests and modules that gate features."""
    return _version_satisfies(version, requirement)
