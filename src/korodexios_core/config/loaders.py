"""Readers for the config file (YAML/JSON) and the .env layer."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..constants import (
    ENCODING,
    ENV_LIST_SEPARATOR,
    ENV_NESTED_SEPARATOR,
    ENV_PREFIX,
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


def normalize_env_key(key: str, prefix: str = ENV_PREFIX) -> str | None:
    """Translate an environment variable name into a dotted config key."""
    cleaned = key.strip()
    if not cleaned:
        return None
    token = prefix.strip().upper()
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


def load_dotenv(path: Path, prefix: str = ENV_PREFIX) -> dict[str, str]:
    """Parse a .env file into flat dotted keys, ignoring foreign prefixes."""
    text = read_text(path)
    values: dict[str, str] = {}
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
        dotted = normalize_env_key(key, prefix)
        if dotted is None:
            continue
        values[dotted] = _unquote_env_value(raw_value.strip())
    return values


def load_environment(environ: Mapping[str, str], prefix: str = ENV_PREFIX) -> dict[str, str]:
    """Collect chassis keys from a live environment mapping."""
    values: dict[str, str] = {}
    for key, value in environ.items():
        dotted = normalize_env_key(str(key), prefix)
        if dotted is None:
            continue
        values[dotted] = str(value)
    return values


def split_list_value(value: str) -> list[str]:
    """Split a scalar environment value into list items."""
    return [item.strip() for item in value.split(ENV_LIST_SEPARATOR) if item.strip()]
