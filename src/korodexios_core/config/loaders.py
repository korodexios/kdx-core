"""Readers for the config file (YAML/JSON) and the .env layer."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..constants import (
    ENCODING,
    ENV_LIST_SEPARATOR,
    ENV_NESTED_SEPARATOR,
    ENV_PREFIX,
    ENV_PREFIX_ALIASES,
    KEY_SEPARATOR,
)
from ..exceptions import ConfigFileError

_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_COMMENT_MARKERS = (" #", "\t#")


def read_text(path: Path) -> str:
    """Read *path* as explicit UTF-8 text, mapping failures to ConfigFileError."""
    try:
        return path.read_text(encoding=ENCODING)
    except OSError as exc:
        raise ConfigFileError(
            f"cannot read {path}: {exc}", hint="Check the path and permissions."
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigFileError(
            f"{path} is not valid UTF-8: {exc}",
            hint="Save the file as UTF-8 (no BOM, no UTF-16).",
        ) from exc


def flatten_mapping(mapping: Mapping[str, Any], prefix: str | None = None) -> dict[str, Any]:
    """Flatten nested mappings into dotted keys, keeping sequences intact."""
    flat: dict[str, Any] = {}
    for raw_key, value in mapping.items():
        key = str(raw_key)
        dotted = f"{prefix}{KEY_SEPARATOR}{key}" if prefix else key
        if isinstance(value, Mapping):
            flat.update(flatten_mapping({str(k): v for k, v in value.items()}, dotted))
        elif isinstance(value, (list, tuple)):
            flat[dotted] = list(value)
        else:
            flat[dotted] = value
    return flat


def unflatten_mapping(flat: Mapping[str, Any]) -> dict[str, Any]:
    """Expand dotted keys back into a nested mapping (the inverse of flattening)."""
    nested: dict[str, Any] = {}
    for key, value in flat.items():
        parts = [part for part in str(key).split(KEY_SEPARATOR) if part]
        if not parts:
            continue
        cursor = nested
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = value
    return nested


def load_config_file(path: Path) -> dict[str, Any]:
    """Load a YAML or JSON config file into flat dotted keys."""
    suffix = path.suffix.lower()
    text = read_text(path)
    if suffix in (".yml", ".yaml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ConfigFileError(
                "PyYAML is required to read YAML configuration files",
                hint="Install the dependency: pip install pyyaml.",
            ) from exc
        try:
            data: Any = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigFileError(
                f"{path.name} is not valid YAML: {exc}",
                hint="Check indentation; YAML forbids tabs.",
            ) from exc
    elif suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigFileError(
                f"{path.name} is not valid JSON: {exc}",
                hint="Check commas, quotes, and trailing brackets.",
            ) from exc
    else:
        raise ConfigFileError(
            f"unsupported config file extension {suffix or '(none)'} for {path}",
            hint="Use a .yml, .yaml, or .json file.",
        )
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigFileError(
            f"{path.name} must hold a mapping at the top level, got {type(data).__name__}",
            hint="Top level keys are 'core' and 'modules'.",
        )
    return flatten_mapping({str(key): value for key, value in data.items()})


def env_prefixes(prefix: str | Sequence[str] | None = None) -> tuple[str, ...]:
    """Return the environment prefixes to consult, in precedence order.

    The built-in aliases are consulted together, in order: KDX_ (primary),
    KORODEXIOS_ (canonical long name). An operator who sets a
    private prefix such as core.env.prefix: MYAPP gets exactly MYAPP_ - no
    implicit aliases - so a shared machine never leaks another project's keys.
    """
    if prefix is None:
        return ENV_PREFIX_ALIASES
    if isinstance(prefix, str):
        token = prefix.strip().upper()
        if not token or token in ENV_PREFIX_ALIASES:
            return ENV_PREFIX_ALIASES
        return (token,)
    resolved = tuple(str(item).strip().upper() for item in prefix if str(item).strip())
    return resolved or ENV_PREFIX_ALIASES


def normalize_exact_key(key: str, token: str) -> str | None:
    """Translate *key* through exactly one prefix *token*, with no alias expansion."""
    cleaned = key.strip()
    if not cleaned:
        return None
    upper = cleaned.upper()
    if token:
        if not upper.startswith(f"{token}_"):
            return None
        remainder = upper[len(token) + 1 :]
    else:
        remainder = upper
    if not remainder:
        return None
    segments = [
        segment.strip().lower().replace("-", "_")
        for segment in remainder.split(ENV_NESTED_SEPARATOR)
        if segment.strip()
    ]
    if not segments:
        return None
    return KEY_SEPARATOR.join(segments)


def normalize_env_key(
    key: str,
    prefix: str | Sequence[str] = ENV_PREFIX,
) -> str | None:
    """Translate an environment variable name into a dotted config key.

    The first prefix that matches wins, so KDX_CORE__LOG__LEVEL and
    KORODEXIOS_CORE__LOG__LEVEL both map to core.log.level.
    """
    for token in env_prefixes(prefix):
        dotted = normalize_exact_key(key, token)
        if dotted is not None:
            return dotted
    return None


def parse_env_alias(text: str) -> tuple[str, str] | None:
    """Parse one alias declaration 'LEGACY_NAME=core.ipc.token'.

    The left side is an environment variable name, the right side the canonical
    chassis key it feeds; a declaration that names itself is dropped.
    """
    cleaned = str(text).strip()
    if not cleaned or "=" not in cleaned:
        return None
    left, right = cleaned.split("=", 1)
    name = left.strip().upper().replace("-", "_")
    dotted = KEY_SEPARATOR.join(
        part.strip().lower().replace("-", "_")
        for part in right.split(KEY_SEPARATOR)
        if part.strip()
    )
    if not _ENV_KEY_PATTERN.match(name) or not dotted:
        return None
    if name.lower() == dotted.replace(KEY_SEPARATOR, "_"):
        return None
    return name, dotted


def normalize_alias_mapping(data: Any) -> dict[str, str]:
    """Return {ENV_VAR: dotted.key} from a configuration value.

    Accepts a mapping (the natural YAML shape) or a list of 'NAME=key' strings
    (the natural CLI and environment shape). Environment names are upper-cased;
    malformed entries are skipped so one typo never breaks the boot.
    """
    aliases: dict[str, str] = {}
    if isinstance(data, Mapping):
        for name, value in data.items():
            cleaned = str(name).strip().upper().replace("-", "_")
            dotted = str(value).strip().lower()
            if cleaned and dotted and _ENV_KEY_PATTERN.match(cleaned):
                aliases[cleaned] = dotted
        return aliases
    if isinstance(data, str):
        data = [item for item in data.split(",") if item.strip()]
    if isinstance(data, (list, tuple)):
        for item in data:
            parsed = parse_env_alias(str(item))
            if parsed is not None:
                aliases[parsed[0]] = parsed[1]
    return aliases


def apply_env_aliases(
    pairs: Sequence[tuple[str, str]],
    aliases: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    """Map *pairs* through *aliases*; return {dotted_key: (value, origin)}.

    The first alias that supplies a key wins, and the origin names the variable
    that carried the value, which is what keeps provenance honest for a legacy
    name with its own prefix.
    """
    resolved: dict[str, tuple[str, str]] = {}
    wanted = {str(name).strip().upper(): str(key) for name, key in aliases.items()}
    for name, value in pairs:
        cleaned = str(name).strip().upper()
        dotted = wanted.get(cleaned)
        if dotted is None or dotted in resolved:
            continue
        resolved[dotted] = (str(value), f"environment alias {cleaned}")
    return resolved


def load_env_aliases(
    environ: Mapping[str, str],
    aliases: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    """Apply *aliases* to a live environment mapping."""
    if not aliases:
        return {}
    return apply_env_aliases([(str(key), str(value)) for key, value in environ.items()], aliases)


def _strip_inline_comment(value: str) -> str:
    for marker in _COMMENT_MARKERS:
        index = value.find(marker)
        if index != -1:
            return value[:index].rstrip()
    return value


def _unquote_env_value(value: str) -> str:
    """Strip surrounding quotes and inline comments from a .env value."""
    if value and value[0] in (chr(39), chr(34)):
        quote = value[0]
        closing = value.find(quote, 1)
        if closing == -1:
            return value[1:].strip()
        inner = value[1:closing]
        if quote == '"':
            return (
                inner.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        return inner
    return _strip_inline_comment(value)


def parse_dotenv_pairs(path: Path) -> list[tuple[str, str]]:
    """Read *path* into (NAME, raw value) pairs, rejecting malformed lines."""
    text = read_text(path)
    pairs: list[tuple[str, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ConfigFileError(
                f"{path.name}:{lineno} is not a KEY=VALUE pair: {line!r}",
                hint="Write one assignment per line; leave blank lines out.",
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_PATTERN.match(key):
            raise ConfigFileError(
                f"{path.name}:{lineno} has an invalid key name: {key!r}",
                hint="Keys use letters, digits, underscores, dots, and dashes.",
            )
        pairs.append((key, raw_value.strip()))
    return pairs


def load_dotenv(
    path: Path,
    prefix: str = ENV_PREFIX,
    aliases: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Parse a .env file into flat dotted keys, ignoring foreign prefixes.

    Aliased names are read first, so a properly prefixed variable always wins
    over a legacy name that was mapped to the same key.
    """
    return load_dotenv_layer(path, prefix, aliases)[0]


def load_dotenv_layer(
    path: Path,
    prefix: str = ENV_PREFIX,
    aliases: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read a .env file into (values, alias origins).

    Aliased names are read first so a properly prefixed variable always wins; the
    origin map then names the legacy variable only where it actually supplied the
    value.
    """
    pairs = parse_dotenv_pairs(path)
    values: dict[str, str] = {}
    origins: dict[str, str] = {}
    for aliased, (raw_value, origin) in apply_env_aliases(pairs, aliases or {}).items():
        values[aliased] = _unquote_env_value(raw_value)
        origins[aliased] = origin
    for token in env_prefixes(prefix):
        for key, raw_value in pairs:
            dotted = normalize_exact_key(key, token)
            if dotted is None or dotted in values:
                continue
            values[dotted] = _unquote_env_value(raw_value)
            origins.pop(dotted, None)
    return values, origins


def load_environment_layer(
    environ: Mapping[str, str],
    prefix: str = ENV_PREFIX,
    aliases: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read a live environment into (values, alias origins).

    A variable that already spells a chassis key under the configured prefix wins
    over an alias mapped to the same key, which keeps migration additive.
    """
    values = load_environment(environ, prefix)
    origins: dict[str, str] = {}
    for dotted, (value, origin) in load_env_aliases(environ, aliases or {}).items():
        if dotted in values:
            continue
        values[dotted] = value
        origins[dotted] = origin
    return values, origins


def load_environment(
    environ: Mapping[str, str],
    prefix: str = ENV_PREFIX,
) -> dict[str, str]:
    """Collect chassis keys from a live environment mapping.

    Prefixes are consulted in precedence order, so KDX_CORE__IPC__HOST wins over a
    KORODEXIOS_CORE__IPC__HOST when both are exported.
    """
    values: dict[str, str] = {}
    for token in env_prefixes(prefix):
        for key, value in environ.items():
            dotted = normalize_exact_key(str(key), token)
            if dotted is not None and dotted not in values:
                values[dotted] = str(value)
    return values


def split_list_value(value: str) -> list[str]:
    """Split a scalar environment value into list items."""
    return [item.strip() for item in value.split(ENV_LIST_SEPARATOR) if item.strip()]
