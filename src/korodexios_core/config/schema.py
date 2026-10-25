"""Declarative module configuration schemas: keys, types, defaults, sensitivity.

A module declares the configuration it expects; the chassis validates that
declaration before and after boot, so a typo or a missing required key is a
pointed diagnostic and never a surprise at runtime.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .layered import (
    LayeredConfig,
    ScopedConfig,
    coerce_bool,
    coerce_float,
    coerce_int,
    coerce_list,
    coerce_path,
    coerce_str,
)

__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "ConfigField",
    "ConfigIssue",
    "ConfigKind",
    "ConfigSchema",
    "schema_from_mapping",
    "summarize_issues",
]

SEVERITY_ERROR: str = "error"
SEVERITY_WARNING: str = "warning"
SEVERITY_INFO: str = "info"


class ConfigKind(StrEnum):
    """Value kinds a module may declare for one configuration key."""

    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    PATH = "path"
    LIST = "list"
    ANY = "any"


_COERCERS: dict[str, Any] = {
    ConfigKind.STR: coerce_str,
    ConfigKind.INT: coerce_int,
    ConfigKind.FLOAT: coerce_float,
    ConfigKind.BOOL: coerce_bool,
    ConfigKind.PATH: coerce_path,
    ConfigKind.LIST: coerce_list,
}


def _kind(value: Any) -> ConfigKind:
    """Return the ConfigKind for *value*, accepting the enum or its spelling."""
    if isinstance(value, ConfigKind):
        return value
    text = str(value).strip().lower()
    try:
        return ConfigKind(text)
    except ValueError as exc:
        allowed = ", ".join(kind.value for kind in ConfigKind)
        raise ValueError(f"configuration kind {value!r} must be one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One declared key: what it is, what it defaults to, how sensitive it is."""

    key: str
    kind: ConfigKind = ConfigKind.STR
    default: Any = None
    required: bool = False
    secret: bool = False
    description: str = ""
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ValueError("a configuration field needs a key")
        object.__setattr__(self, "key", str(self.key).strip())
        object.__setattr__(self, "kind", _kind(self.kind))

    def coerce(self, value: Any, *, key: str | None = None, source: str = "schema") -> Any:
        """Return *value* coerced to this field's kind, raising ConfigValueError."""
        coercer = _COERCERS.get(self.kind)
        if coercer is None:
            return value
        return coercer(value, key=key or self.key, source=source)

    def check(self, value: Any) -> str:
        """Return a problem description for *value*, or an empty string when valid."""
        try:
            coerced = self.coerce(value)
        except Exception as exc:  # coercion carries the operator-facing message
            return str(getattr(exc, "message", exc))
        if self.choices and self.kind is not ConfigKind.ANY:
            rendered = str(coerced)
            if rendered not in self.choices:
                return f"{self.key} must be one of {', '.join(self.choices)}, got {rendered!r}"
        return ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the declaration."""
        return {
            "key": self.key,
            "type": self.kind.value,
            "default": self.default,
            "required": self.required,
            "secret": self.secret,
            "description": self.description,
            "choices": list(self.choices),
        }


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """One validation finding against a declared schema."""

    key: str
    message: str
    severity: str = SEVERITY_ERROR
    source: str = ""
    module: str = ""

    def describe(self) -> str:
        """Return the one-line rendering used by 'kdx config check'."""
        where = f" [{self.source}]" if self.source else ""
        owner = f"{self.module}: " if self.module else ""
        return f"{owner}{self.message}{where}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the finding."""
        return {
            "key": self.key,
            "message": self.message,
            "severity": self.severity,
            "source": self.source,
            "module": self.module,
        }


@dataclass(frozen=True, slots=True)
class ConfigSchema:
    """The configuration a module declares, validated before it runs."""

    module: str = ""
    fields: tuple[ConfigField, ...] = ()

    def __post_init__(self) -> None:
        keys = [field.key for field in self.fields]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(
                f"module {self.module or 'schema'} declares duplicate keys: "
                + ", ".join(duplicates)
            )

    @property
    def is_empty(self) -> bool:
        """Return True when the module declares nothing."""
        return not self.fields

    def keys(self) -> tuple[str, ...]:
        """Return the declared keys in declaration order."""
        return tuple(field.key for field in self.fields)

    def secrets(self) -> tuple[str, ...]:
        """Return the declared keys marked sensitive."""
        return tuple(field.key for field in self.fields if field.secret)

    def required_keys(self) -> tuple[str, ...]:
        """Return the declared keys a project must set."""
        return tuple(field.key for field in self.fields if field.required)

    def field_for(self, key: str) -> ConfigField | None:
        """Return the declaration for *key*, when the schema declares it."""
        for field in self.fields:
            if field.key == str(key):
                return field
        return None

    def scoped_values(self, config: Any, prefix: str | None = None) -> dict[str, Any]:
        """Return the values this module would read for its own keys.

        Mirrors the ScopedConfig lookup order - prefixed key first, then the bare
        global key - so validation sees exactly what the module would read.
        """
        root = prefix if prefix is not None else (f"modules.{self.module}" if self.module else "")
        values: dict[str, Any] = {}
        if isinstance(config, LayeredConfig | ScopedConfig):
            subtree = (
                config.child(root).as_dict()
                if root and isinstance(config, LayeredConfig)
                else config.as_dict()
            )
            for field in self.fields:
                if field.key in subtree:
                    values[field.key] = subtree[field.key]
                elif isinstance(config, LayeredConfig) and field.key in config:
                    values[field.key] = config.get(field.key)
        elif isinstance(config, Mapping):
            for field in self.fields:
                if field.key in config:
                    values[field.key] = config[field.key]
        return values

    def validate(
        self,
        config: Any = None,
        *,
        prefix: str | None = None,
        values: Mapping[str, Any] | None = None,
        origins: Mapping[str, str] | None = None,
    ) -> list[ConfigIssue]:
        """Validate *config* against the declared fields; return the findings."""
        if self.is_empty:
            return []
        resolved = values if values is not None else self.scoped_values(config, prefix=prefix)
        root = prefix if prefix is not None else (f"modules.{self.module}" if self.module else "")
        issues: list[ConfigIssue] = []
        for field in self.fields:
            full = f"{root}.{field.key}" if root else field.key
            source = str((origins or {}).get(field.key, "") or "")
            if field.key not in resolved:
                if field.required and field.default is None:
                    issues.append(
                        ConfigIssue(
                            key=full,
                            message=f"{field.key} is required by module "
                            f"{self.module or 'chassis'} and has no default",
                            severity=SEVERITY_ERROR,
                            source=source,
                            module=self.module,
                        )
                    )
                continue
            problem = field.check(resolved[field.key])
            if problem:
                issues.append(
                    ConfigIssue(
                        key=full,
                        message=problem,
                        severity=SEVERITY_ERROR,
                        source=source,
                        module=self.module,
                    )
                )
        return issues

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the schema."""
        return {"module": self.module, "fields": [field.as_dict() for field in self.fields]}


def _field_from_table(key: str, table: Mapping[str, Any]) -> ConfigField:
    """Build one field from a manifest table."""
    kind = table.get("type", table.get("kind", ConfigKind.STR))
    choices = table.get("choices", ())
    if isinstance(choices, str):
        choices = (choices,)
    return ConfigField(
        key=key,
        kind=_kind(kind),
        default=table.get("default"),
        required=bool(table.get("required", False)),
        secret=bool(table.get("secret", False)),
        description=str(table.get("description", "") or ""),
        choices=tuple(str(item) for item in choices),
    )


def schema_from_mapping(
    data: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    /,
    *,
    module: str = "",
) -> ConfigSchema:
    """Build a schema from manifest tables, the shape a kdx_module.toml can carry.

    Accepts a list of field tables, or a mapping of key to either a compact type
    string ('int') or a field table, so a manifest never invents a second language.
    """
    fields: list[ConfigField] = []
    if isinstance(data, Mapping):
        declared = data.get("fields")
        if isinstance(declared, list):
            for entry in declared:
                if isinstance(entry, Mapping):
                    fields.append(_field_from_table(str(entry.get("key") or ""), entry))
        else:
            for key, value in data.items():
                if isinstance(value, Mapping):
                    fields.append(_field_from_table(str(key), value))
                else:
                    fields.append(ConfigField(key=str(key), kind=_kind(value)))
    else:
        for entry in data:
            if isinstance(entry, Mapping):
                key = str(entry.get("key") or "").strip()
                if key:
                    fields.append(_field_from_table(key, entry))
    return ConfigSchema(module=module, fields=tuple(fields))


def summarize_issues(issues: Iterable[ConfigIssue]) -> dict[str, int]:
    """Return how many findings sit at each severity."""
    counts = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 0, SEVERITY_INFO: 0}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
    return counts
