"""Layered configuration with strict precedence: CLI > environment > file > defaults."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, cast

from ..constants import ENV_PREFIX, KEY_SEPARATOR
from ..exceptions import ConfigurationError, ConfigValueError
from . import keys as key_rules
from .defaults import CORE_DEFAULTS, with_runtime_defaults
from .loaders import (
    load_config_file,
    load_dotenv,
    load_environment,
    split_list_value,
    unflatten_mapping,
)

_TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "on", "enabled"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "n", "off", "disabled", ""})


class MISSING:
    """Sentinel meaning 'no caller default supplied'."""


class ConfigSource(IntEnum):
    """Configuration layers, ordered from lowest to highest precedence."""

    DEFAULT = 0
    FILE = 1
    LOCAL = 2
    ENV = 3
    CLI = 4

    def label(self) -> str:
        """Return the human label used by 'kdx config show'."""
        return _SOURCE_LABELS[self]


_SOURCE_LABELS: dict[ConfigSource, str] = {
    ConfigSource.DEFAULT: "built-in default",
    ConfigSource.FILE: "config file",
    ConfigSource.LOCAL: "local override file",
    ConfigSource.ENV: "environment",
    ConfigSource.CLI: "CLI argument",
}


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    """A resolved value plus the layer and origin it came from."""

    key: str
    value: Any
    source: ConfigSource
    origin: str

    def as_row(self) -> tuple[str, str, str]:
        """Return (key, rendered value, source label) for CLI tables."""
        return (self.key, _render_value(self.value), self.source.label())


def _render_value(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "(empty)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _fail(key: str, value: Any, source: str, expected: str, hint: str) -> ConfigValueError:
    return ConfigValueError(
        f"{key} must be {expected}, got {_render_value(value)!r}",
        key=key,
        value=value,
        source=source,
        hint=hint,
    )


def coerce_bool(value: Any, *, key: str, source: str) -> bool:
    """Coerce *value* to a bool, rejecting anything ambiguous."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        raise _fail(
            key, value, source, "a boolean (true/false/yes/no/on/off)", "Use true or false."
        )
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise _fail(key, value, source, "a boolean (0/1)", "Use 0 or 1.")
    raise _fail(key, value, source, "a boolean", "Use true or false.")


def coerce_int(value: Any, *, key: str, source: str) -> int:
    """Coerce *value* to an int, accepting clean numeric strings."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise _fail(key, value, source, "a whole number", "For example: 1048576.") from exc
    raise _fail(key, value, source, "a whole number", "For example: 1048576.")


def coerce_float(value: Any, *, key: str, source: str) -> float:
    """Coerce *value* to a float, accepting numeric strings."""
    if isinstance(value, bool):
        raise _fail(key, value, source, "a number", "Use a decimal value such as 1.5.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise _fail(key, value, source, "a number", "For example: 2.5.") from exc
    raise _fail(key, value, source, "a number", "For example: 2.5.")


def coerce_str(value: Any, *, key: str, source: str) -> str:
    """Coerce *value* to a string."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (int, float, bool, Path)):
        return str(value)
    raise _fail(key, value, source, "a string", "Wrap the value in quotes in the config file.")


def coerce_path(value: Any, *, key: str, source: str) -> Path:
    """Coerce *value* to a pathlib.Path without touching the filesystem."""
    if isinstance(value, Path):
        return value
    text = coerce_str(value, key=key, source=source)
    if not text.strip():
        raise _fail(key, value, source, "a filesystem path", "Provide a non-empty path.")
    return Path(text).expanduser()


def coerce_list(value: Any, *, key: str, source: str) -> list[Any]:
    """Coerce *value* to a list, splitting comma-separated scalars."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    if isinstance(value, str):
        return split_list_value(value)
    raise _fail(key, value, source, "a list or comma-separated string", "For example: a,b,c.")


@dataclass(frozen=True, slots=True)
class _Layer:
    values: dict[str, Any]
    source: ConfigSource
    origin: str


class LayeredConfig:
    """Read-only view over five configuration layers with strict precedence.

    Precedence, lowest to highest: built-in defaults, the project config file, the
    gitignored local override (kdx.local.yml), the environment (.env plus process
    env), and CLI overrides.
    """

    def __init__(
        self,
        defaults: Mapping[str, Any] | None = None,
        file_values: Mapping[str, Any] | None = None,
        env_values: Mapping[str, Any] | None = None,
        cli_values: Mapping[str, Any] | None = None,
        *,
        local_values: Mapping[str, Any] | None = None,
        prefix: str = ENV_PREFIX,
        config_file: Path | None = None,
        local_config_file: Path | None = None,
        env_file: Path | None = None,
    ) -> None:
        base = dict(CORE_DEFAULTS)
        if defaults:
            base.update({str(key): value for key, value in defaults.items()})
        self._layers: list[_Layer] = [
            _Layer(values=base, source=ConfigSource.DEFAULT, origin="defaults.py"),
            _Layer(
                values=dict(file_values or {}),
                source=ConfigSource.FILE,
                origin=str(config_file) if config_file else "(no config file)",
            ),
            _Layer(
                values=dict(local_values or {}),
                source=ConfigSource.LOCAL,
                origin=str(local_config_file) if local_config_file else "(no local override)",
            ),
            _Layer(
                values=dict(env_values or {}),
                source=ConfigSource.ENV,
                origin=str(env_file) if env_file else "process environment",
            ),
            _Layer(
                values=dict(cli_values or {}),
                source=ConfigSource.CLI,
                origin="command line",
            ),
        ]
        self._index = {layer.source: position for position, layer in enumerate(self._layers)}
        self._prefix = prefix
        self._config_file = config_file
        self._local_config_file = local_config_file
        self._env_file = env_file

    @classmethod
    def load(
        cls,
        *,
        config_file: Path | None = None,
        local_config_file: Path | None = None,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
        cli_overrides: Mapping[str, Any] | None = None,
        runtime_defaults: Mapping[str, Any] | None = None,
        prefix: str | None = None,
    ) -> LayeredConfig:
        """Load the file, local override, and environment layers, then build the view."""
        import os

        env = os.environ if environ is None else environ
        file_values = load_config_file(config_file) if config_file else {}
        local_values = load_config_file(local_config_file) if local_config_file else {}
        resolved_prefix = (
            prefix
            or _prefix_from(local_values)
            or _prefix_from(file_values)
            or str(CORE_DEFAULTS["core.env.prefix"])
        )
        dotenv_values = load_dotenv(env_file, resolved_prefix) if env_file else {}
        live_values = load_environment(env, resolved_prefix)
        env_layer: dict[str, Any] = dict(dotenv_values)
        env_layer.update(live_values)  # a real environment variable beats the .env file
        return cls(
            defaults=with_runtime_defaults(runtime_defaults or {}),
            file_values=file_values,
            env_values=env_layer,
            cli_values=cli_overrides,
            local_values=local_values,
            prefix=resolved_prefix,
            config_file=config_file,
            local_config_file=local_config_file,
            env_file=env_file,
        )

    @property
    def prefix(self) -> str:
        """Return the environment-variable prefix in force."""
        return self._prefix

    @property
    def config_file(self) -> Path | None:
        """Return the config file that was loaded, when any."""
        return self._config_file

    @property
    def local_config_file(self) -> Path | None:
        """Return the gitignored local override that was loaded, when any."""
        return self._local_config_file

    @property
    def env_file(self) -> Path | None:
        """Return the .env file that was loaded, when any."""
        return self._env_file

    def values_of(self, source: ConfigSource) -> dict[str, Any]:
        """Return a copy of the values held by the *source* layer."""
        return dict(self._layer(source).values)

    def _layer(self, source: ConfigSource) -> _Layer:
        return self._layers[self._index[source]]

    def with_cli_overrides(self, values: Mapping[str, Any]) -> LayeredConfig:
        """Return a copy whose CLI layer also contains *values*."""
        merged = self.values_of(ConfigSource.CLI)
        merged.update({str(key): value for key, value in values.items()})
        return self._rebuild(cli_values=merged)

    def with_defaults(self, values: Mapping[str, Any]) -> LayeredConfig:
        """Return a copy whose default layer also contains *values*."""
        merged = self.values_of(ConfigSource.DEFAULT)
        merged.update({str(key): value for key, value in values.items()})
        return self._rebuild(defaults=merged)

    def _rebuild(
        self,
        defaults: Mapping[str, Any] | None = None,
        cli_values: Mapping[str, Any] | None = None,
    ) -> LayeredConfig:
        return LayeredConfig(
            defaults=defaults if defaults is not None else self.values_of(ConfigSource.DEFAULT),
            file_values=self.values_of(ConfigSource.FILE),
            env_values=self.values_of(ConfigSource.ENV),
            cli_values=cli_values if cli_values is not None else self.values_of(ConfigSource.CLI),
            local_values=self.values_of(ConfigSource.LOCAL),
            prefix=self._prefix,
            config_file=self._config_file,
            local_config_file=self._local_config_file,
            env_file=self._env_file,
        )

    def _lookup(self, key: str) -> tuple[bool, ConfigEntry | None]:
        dotted = KEY_SEPARATOR.join(part for part in str(key).split(KEY_SEPARATOR) if part)
        if not dotted:
            return False, None
        for layer in reversed(self._layers):
            if dotted in layer.values:
                return True, ConfigEntry(dotted, layer.values[dotted], layer.source, layer.origin)
        return False, None

    def resolve(self, key: str) -> ConfigEntry:
        """Resolve *key* to an entry, raising when no layer supplies it."""
        found, entry = self._lookup(key)
        if not found or entry is None:
            raise ConfigurationError(
                f"configuration key {str(key)!r} is not defined by any layer",
                hint="Read optional keys with get() instead of require().",
            )
        return entry

    def get(self, key: str, default: Any = None) -> Any:
        """Return the winning value for *key*, or *default* when unset everywhere."""
        found, entry = self._lookup(key)
        if not found or entry is None:
            return default
        return entry.value

    def require(self, key: str) -> Any:
        """Return the winning value for *key* or raise ConfigurationError."""
        return self.resolve(key).value

    def source_of(self, key: str) -> ConfigSource | None:
        """Return the layer that wins for *key*, or None when the key is unknown."""
        found, entry = self._lookup(key)
        return entry.source if found and entry else None

    def origin_of(self, key: str) -> str:
        """Return the human-readable origin that wins for *key*."""
        found, entry = self._lookup(key)
        return entry.origin if found and entry else "unknown"

    def get_bool(self, key: str, default: Any = None) -> bool:
        """Return *key* coerced to bool."""
        return cast(bool, self._typed(key, default, coerce_bool, "a boolean"))

    def get_int(self, key: str, default: Any = None) -> int:
        """Return *key* coerced to int."""
        return cast(int, self._typed(key, default, coerce_int, "a whole number"))

    def get_float(self, key: str, default: Any = None) -> float:
        """Return *key* coerced to float."""
        return cast(float, self._typed(key, default, coerce_float, "a number"))

    def get_str(self, key: str, default: Any = None) -> str:
        """Return *key* coerced to str."""
        return cast(str, self._typed(key, default, coerce_str, "a string"))

    def get_path(self, key: str, default: Any = None) -> Path:
        """Return *key* coerced to Path."""
        return cast(Path, self._typed(key, default, coerce_path, "a filesystem path"))

    def get_list(self, key: str, default: Any = None) -> list[Any]:
        """Return *key* coerced to a list."""
        return cast(list[Any], self._typed(key, default, coerce_list, "a list"))

    def _typed(
        self,
        key: str,
        default: Any,
        coercer: Callable[..., Any],
        expected: str,
    ) -> Any:
        found, entry = self._lookup(key)
        if not found or entry is None:
            value = _fallback_for(coercer) if default is None else default
            return coercer(value, key=key, source="built-in default")
        return coercer(entry.value, key=key, source=entry.source.label())

    def keys(self) -> list[str]:
        """Return every key mentioned by any layer, sorted."""
        seen: set[str] = set()
        for layer in self._layers:
            seen.update(layer.values.keys())
        return sorted(seen)

    def unknown_keys(self, module_names: Iterable[str] = ()) -> list[tuple[str, str]]:
        """Return (key, suggestion) pairs for keys nothing in the chassis reads."""
        return key_rules.unknown_keys(self.keys(), module_names)

    def project_keys(self) -> tuple[str, ...]:
        """Return the project-owned keys among every layer."""
        return key_rules.project_key_names(self.keys())

    def as_dict(self) -> dict[str, Any]:
        """Return the merged flat view, highest precedence winning."""
        merged: dict[str, Any] = {}
        for layer in self._layers:
            merged.update(layer.values)
        return merged

    def as_nested(self) -> dict[str, Any]:
        """Return the merged view expanded back into nested mappings."""
        return unflatten_mapping(self.as_dict())

    def entries(self) -> list[ConfigEntry]:
        """Return one resolved entry per known key, sorted by key."""
        resolved: list[ConfigEntry] = []
        for key in self.keys():
            found, entry = self._lookup(key)
            if found and entry is not None:
                resolved.append(entry)
        return resolved

    def describe_rows(self, limit: int | None = None) -> list[tuple[str, str, str]]:
        """Return (key, value, source) rows for 'kdx config show'."""
        rows = [entry.as_row() for entry in self.entries()]
        return rows[:limit] if limit else rows

    def child(self, prefix: str) -> ScopedConfig:
        """Return a scoped view rooted at *prefix* (for example 'modules.backup')."""
        return ScopedConfig(self, prefix)

    def __contains__(self, key: object) -> bool:
        found, _ = self._lookup(str(key))
        return found

    def __repr__(self) -> str:
        return (
            f"LayeredConfig(keys={len(self.keys())}, config_file={self._config_file!s}, "
            f"local_file={self._local_config_file!s}, env_file={self._env_file!s}, "
            f"prefix={self._prefix!r})"
        )


def _prefix_from(file_values: Mapping[str, Any]) -> str | None:
    value = file_values.get("core.env.prefix")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def _fallback_for(coercer: Callable[..., Any]) -> Any:
    if coercer is coerce_bool:
        return False
    if coercer is coerce_int:
        return 0
    if coercer is coerce_float:
        return 0.0
    if coercer is coerce_list:
        return []
    if coercer is coerce_path:
        return Path(".")
    return ""


class ScopedConfig:
    """Prefix-scoped configuration view handed to individual modules."""

    def __init__(self, parent: LayeredConfig, prefix: str) -> None:
        self._parent = parent
        self._prefix = prefix.strip(KEY_SEPARATOR)

    @property
    def prefix(self) -> str:
        """Return the dotted prefix this view is rooted at."""
        return self._prefix

    @property
    def parent(self) -> LayeredConfig:
        """Return the underlying layered configuration."""
        return self._parent

    def _candidates(self, key: str) -> tuple[str, str]:
        clean = str(key).strip(KEY_SEPARATOR)
        return (f"{self._prefix}{KEY_SEPARATOR}{clean}" if self._prefix else clean, clean)

    def resolve(self, key: str) -> ConfigEntry:
        """Resolve *key* inside the prefix, falling back to the global key."""
        scoped, bare = self._candidates(key)
        for candidate in (scoped, bare):
            found, entry = self._parent._lookup(candidate)
            if found and entry is not None:
                return entry
        raise ConfigurationError(
            f"configuration key {scoped!r} is not defined by any layer",
            hint="Add the key under the matching 'modules' section of kdx.yml.",
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, preferring the prefixed form."""
        scoped, bare = self._candidates(key)
        for candidate in (scoped, bare):
            found, entry = self._parent._lookup(candidate)
            if found and entry is not None:
                return entry.value
        return default

    def require(self, key: str) -> Any:
        """Return the value for *key* or raise ConfigurationError."""
        return self.resolve(key).value

    def source_of(self, key: str) -> ConfigSource | None:
        """Return the winning layer for *key*."""
        scoped, bare = self._candidates(key)
        for candidate in (scoped, bare):
            found, entry = self._parent._lookup(candidate)
            if found and entry is not None:
                return entry.source
        return None

    def get_bool(self, key: str, default: Any = None) -> bool:
        """Return *key* coerced to bool."""
        return cast(bool, self._typed(key, default, coerce_bool))

    def get_int(self, key: str, default: Any = None) -> int:
        """Return *key* coerced to int."""
        return cast(int, self._typed(key, default, coerce_int))

    def get_float(self, key: str, default: Any = None) -> float:
        """Return *key* coerced to float."""
        return cast(float, self._typed(key, default, coerce_float))

    def get_str(self, key: str, default: Any = None) -> str:
        """Return *key* coerced to str."""
        return cast(str, self._typed(key, default, coerce_str))

    def get_path(self, key: str, default: Any = None) -> Path:
        """Return *key* coerced to Path."""
        return cast(Path, self._typed(key, default, coerce_path))

    def get_list(self, key: str, default: Any = None) -> list[Any]:
        """Return *key* coerced to a list."""
        return cast(list[Any], self._typed(key, default, coerce_list))

    def _typed(self, key: str, default: Any, coercer: Callable[..., Any]) -> Any:
        scoped, bare = self._candidates(key)
        for candidate in (scoped, bare):
            found, entry = self._parent._lookup(candidate)
            if found and entry is not None:
                return coercer(entry.value, key=entry.key, source=entry.source.label())
        value = _fallback_for(coercer) if default is None else default
        return coercer(value, key=scoped, source="default")

    def keys(self) -> list[str]:
        """Return keys defined under this prefix, stripped of the prefix."""
        prefix_with_dot = f"{self._prefix}{KEY_SEPARATOR}"
        stripped: list[str] = []
        for key in self._parent.keys():  # noqa: SIM118 - LayeredConfig is not a Mapping
            if key.startswith(prefix_with_dot):
                stripped.append(key[len(prefix_with_dot) :])
        return sorted(stripped)

    def as_dict(self) -> dict[str, Any]:
        """Return the sub-tree under this prefix as a flat mapping."""
        prefix_with_dot = f"{self._prefix}{KEY_SEPARATOR}"
        data: dict[str, Any] = {}
        for key, value in self._parent.as_dict().items():
            if key.startswith(prefix_with_dot):
                data[key[len(prefix_with_dot) :]] = value
        return data

    def __contains__(self, key: object) -> bool:
        scoped, bare = self._candidates(str(key))
        for candidate in (scoped, bare):
            found, _ = self._parent._lookup(candidate)
            if found:
                return True
        return False

    def __repr__(self) -> str:
        return f"ScopedConfig(prefix={self._prefix!r}, keys={self.keys()})"


_UNSET = object()
