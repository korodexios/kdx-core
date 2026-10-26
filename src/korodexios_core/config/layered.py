"""Layered configuration with strict precedence: CLI > environment > file > defaults."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, cast

from ..constants import (
    ENV_PREFIX,
    ENV_PREFIX_ALIASES,
    KEY_SEPARATOR,
    REDACTION,
    SECRET_KEY_COMPOUNDS,
    SECRET_KEY_SEGMENTS,
    SECRET_KEY_WORDS,
)
from ..exceptions import ConfigurationError, ConfigValueError
from . import keys as key_rules
from .defaults import CORE_DEFAULTS, with_runtime_defaults
from .loaders import (
    load_config_file,
    load_dotenv_layer,
    load_environment_layer,
    normalize_alias_mapping,
    split_list_value,
    unflatten_mapping,
)

ALIAS_KEY: str = "core.env.aliases"
"""The chassis key that maps a legacy environment name to a canonical dotted key."""

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

    #: Not a layer: the marker for a value that fell through a declared sentinel.
    INHERIT = 5

    def label(self) -> str:
        """Return the human label used by 'kdx config show'."""
        return _SOURCE_LABELS[self]


_SOURCE_LABELS: dict[ConfigSource, str] = {
    ConfigSource.DEFAULT: "built-in default",
    ConfigSource.FILE: "config file",
    ConfigSource.LOCAL: "local override file",
    ConfigSource.ENV: "environment",
    ConfigSource.CLI: "CLI argument",
    ConfigSource.INHERIT: "inherit sentinel",
}


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    """A resolved value plus the layer and origin it came from."""

    key: str
    value: Any
    source: ConfigSource
    origin: str

    def as_row(self, redact: bool = False) -> tuple[str, str, str]:
        """Return (key, rendered value, source label) for CLI tables.

        With *redact* on, a sensitive value is replaced by the chassis redaction
        marker, so a report can be pasted into an issue without leaking a secret.
        """
        value = redact_value(self.value) if redact and is_secret_key(self.key) else self.value
        return (self.key, _render_value(value), self.source.label())


def is_secret_key(key: str) -> bool:
    """Return True when *key* names a value that should not be printed in clear.

    A key is sensitive when any of its dot-separated segments names a secret
    (token, password, api_key, ...). Matching runs on whole segments, on the
    snake_case words inside a segment, and on two-word compounds, so
    'core.ipc.auth' stays readable while 'core.ipc.token', 'modules.x.api_key'
    and 'modules.backup.aws_access_key_id' are all masked.
    """
    segments = [part.strip().lower() for part in str(key).split(KEY_SEPARATOR) if part.strip()]
    for segment in segments:
        if segment in SECRET_KEY_SEGMENTS:
            return True
        words = [word for word in segment.split("_") if word]
        if any(word in SECRET_KEY_WORDS for word in words):
            return True
        joined = f"_{'_'.join(words)}_"
        if any(marker in joined for marker in SECRET_KEY_COMPOUNDS):
            return True
    return False


def redact_value(value: Any, *, marker: str = REDACTION) -> Any:
    """Return *marker* for a non-empty string value, otherwise *value* unchanged."""
    if isinstance(value, str) and value.strip():
        return marker
    return value


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
        env_origins: Mapping[str, str] | None = None,
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
        self._env_origins = {
            str(key): str(value) for key, value in (env_origins or {}).items()
        }
        self._key_sentinels: dict[str, Any] = {}
        self._sentinel_cache: tuple[str, ...] | None = None

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
        aliases = _aliases_from(
            cli_overrides, local_values, file_values, env, prefix=resolved_prefix
        )
        dotenv_values, dotenv_origins = (
            load_dotenv_layer(env_file, resolved_prefix, aliases) if env_file else ({}, {})
        )
        live_values, live_origins = load_environment_layer(env, resolved_prefix, aliases)
        env_layer: dict[str, Any] = dict(dotenv_values)
        env_layer.update(live_values)  # a real environment variable beats the .env file
        env_origins = {**dotenv_origins, **live_origins}
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
            env_origins=env_origins,
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

    def env_aliases(self) -> dict[str, str]:
        """Return the environment aliases in force, from either declared shape.

        The snapshot is derived from the non-default layers, so the mapping a
        project writes in `kdx.yml`, the `NAME=key` list an operator exports as
        `KDX_CORE__ENV__ALIASES`, and the `--env-alias` flag all arrive as one
        `{ENV_VAR: dotted.key}` mapping a report can print without learning
        which shape the caller happened to use.
        """
        merged: dict[str, str] = {}
        for source in (
            ConfigSource.FILE,
            ConfigSource.LOCAL,
            ConfigSource.ENV,
            ConfigSource.CLI,
        ):
            merged.update(alias_entries(self._layer(source).values))
        return merged

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
            env_origins=dict(self._env_origins),
        )

    def _lookup(self, key: str) -> tuple[bool, ConfigEntry | None]:
        dotted = KEY_SEPARATOR.join(part for part in str(key).split(KEY_SEPARATOR) if part)
        if not dotted:
            return False, None
        sentinel: ConfigEntry | None = None
        for layer in reversed(self._layers):
            if dotted not in layer.values:
                continue
            value = layer.values[dotted]
            origin = layer.origin
            if layer.source is ConfigSource.ENV:
                origin = self._env_origins.get(dotted, layer.origin)
            if sentinel is None and self._sentinel_match(dotted, value) is not None:
                sentinel = ConfigEntry(dotted, value, layer.source, origin)
                continue
            if sentinel is None:
                return True, ConfigEntry(dotted, value, layer.source, origin)
            return True, self._inherited(dotted, value, origin, sentinel)
        if sentinel is not None:
            return True, ConfigEntry(
                dotted, None, ConfigSource.INHERIT,
                f"{ConfigSource.INHERIT.label()} {sentinel.value!r} from {sentinel.origin}",
            )
        return False, None

    @staticmethod
    def _inherited(
        dotted: str, value: Any, origin: str, sentinel: ConfigEntry
    ) -> ConfigEntry:
        """Return the entry a sentinel defers to, naming both layers in the origin."""
        return ConfigEntry(
            dotted, value, ConfigSource.INHERIT,
            f"{ConfigSource.INHERIT.label()} {sentinel.value!r} from "
            f"{sentinel.origin} falls through to {origin}",
        )

    def _sentinel_match(self, key: str, value: Any) -> str | None:
        """Return the token that *value* answers to, when it is a declared sentinel."""
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return stripped if stripped in self.sentinel_tokens(key) else None

    def sentinel_tokens(self, key: str | None = None) -> tuple[str, ...]:
        """Return the sentinel tokens in force, project-wide and for *key*.

        A sentinel is a value one layer writes to say it has no opinion: the chassis
        skips that layer and answers with the layer below, and source_of reports
        'inherit sentinel' instead of the layer that carried the token. A project
        declares its own words for it in core.config.sentinels - the chassis owns no
        token of its own - and a module narrows it per key with the sentinel field of
        its own schema.
        """
        declared = list(self._global_sentinels())
        if key is not None:
            token = self._key_sentinels.get(str(key))
            if isinstance(token, str) and token.strip() and token.strip() not in declared:
                declared.append(token.strip())
        return tuple(declared)

    def declare_sentinels(self, sentinels: Mapping[str, Any]) -> None:
        """Record per-key sentinel tokens, as a module schema declares them."""
        for key, token in dict(sentinels).items():
            if isinstance(token, str) and token.strip():
                self._key_sentinels[str(key)] = token.strip()

    def key_sentinels(self) -> dict[str, str]:
        """Return the per-key sentinel tokens recorded so far."""
        return dict(self._key_sentinels)

    def _global_sentinels(self) -> tuple[str, ...]:
        cached = self._sentinel_cache
        if cached is None:
            values: Any = None
            for layer in reversed(self._layers):
                if "core.config.sentinels" in layer.values:
                    values = layer.values['core.config.sentinels']
                    break
            items: list[Any] = []
            if isinstance(values, str):
                items = list(split_list_value(values))
            elif isinstance(values, (list, tuple, set, frozenset)):
                items = list(values)
            cached = tuple(
                sorted({str(item).strip() for item in items if str(item).strip()})
            )
            self._sentinel_cache = cached
        return cached

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

    def unknown_keys(
        self,
        module_names: Iterable[str] = (),
        known_keys: Iterable[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Return (key, suggestion) pairs for keys nothing in the chassis reads.

        Suggestions are scoped with *known_keys*, which defaults to the keys every layer
        carries, so a typo inside a module namespace is answered with that module's own
        key rather than with an unrelated loaded module.
        """
        present = self.keys()
        return key_rules.unknown_keys(self.keys(), module_names, known_keys or present)

    def project_keys(self) -> tuple[str, ...]:
        """Return the project-owned keys among every layer."""
        return key_rules.project_key_names(self.keys())

    def is_secret(self, key: str) -> bool:
        """Return True when the value behind *key* is sensitive by key name."""
        return is_secret_key(str(key))

    def secret_keys(self) -> tuple[str, ...]:
        """Return the sensitive keys among every layer, sorted."""
        return tuple(key for key in self.keys() if is_secret_key(key))

    def as_dict(self, redact: bool = False) -> dict[str, Any]:
        """Return the merged flat view, highest precedence winning.

        With *redact* on, sensitive string values are replaced by the chassis
        redaction marker so the mapping is safe to print, log, or post.
        """
        merged: dict[str, Any] = {}
        for layer in self._layers:
            merged.update(layer.values)
        if not redact:
            return merged
        return {
            key: redact_value(value) if is_secret_key(key) else value
            for key, value in merged.items()
        }

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

    def describe_rows(
        self,
        limit: int | None = None,
        redact: bool = False,
    ) -> list[tuple[str, str, str]]:
        """Return (key, value, source) rows for 'kdx config show'.

        Redaction is opt-out only in the sense that the CLI passes it on by
        default; the library stays honest by defaulting to the raw value.
        """
        rows = [entry.as_row(redact=redact) for entry in self.entries()]
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


def alias_entries(layer: Mapping[str, Any] | None) -> dict[str, str]:
    """Return the aliases one configuration layer declares, in either shape.

    A layer carries the mapping under the exact key `core.env.aliases` or under
    the flattened leaves `core.env.aliases.NAME` that a YAML mapping becomes on
    its way through `load_config_file`. Leaves win inside one layer, because an
    empty mapping default flattens away to nothing at all.
    """
    if not isinstance(layer, Mapping):
        return {}
    declared = layer.get(ALIAS_KEY)
    merged = normalize_alias_mapping(declared) if declared is not None else {}
    head = f"{ALIAS_KEY}."
    for key, value in layer.items():
        name = str(key)
        if not name.startswith(head):
            continue
        name = name[len(head) :]
        if not name or "." in name:
            continue
        merged.update(normalize_alias_mapping({name: value}))
    return merged


def _alias_variables(prefix: str | None = None) -> tuple[str, ...]:
    """Return the environment variables that may declare aliases, lowest first."""
    tokens = list(ENV_PREFIX_ALIASES)
    resolved = str(prefix or ENV_PREFIX).strip().upper()
    if resolved and resolved not in tokens:
        tokens.append(resolved)
    return tuple(f"{token}_CORE__ENV__ALIASES" for token in tokens)


def _aliases_from(
    cli_values: Mapping[str, Any] | None,
    local_values: Mapping[str, Any],
    file_values: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
    prefix: str | None = None,
) -> dict[str, str]:
    """Merge declared environment aliases, highest precedence last.

    `core.env.aliases` maps a legacy variable name to the canonical chassis key it
    feeds, so a project migrating to the chassis keeps exporting `LEGACY_TOKEN`
    while the chassis reads `core.ipc.token`, with provenance naming the variable
    that actually carried the value. Both shapes are read here: the mapping a
    project writes in `kdx.yml` (which the loader flattens to leaves) and the
    `NAME=key` list an operator exports as `<PREFIX>_CORE__ENV__ALIASES`.
    """
    merged: dict[str, str] = {}
    if environ is not None:
        for variable in _alias_variables(prefix):
            raw = str(environ.get(variable, "")).strip()
            if raw:
                merged.update(normalize_alias_mapping(raw))
    for layer in (file_values, local_values, cli_values):
        merged.update(alias_entries(layer))
    return merged


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
