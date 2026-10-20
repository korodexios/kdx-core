"""Configuration layer: strict precedence across CLI, environment, file, defaults."""

from __future__ import annotations

from .defaults import CORE_DEFAULTS, default_for, with_runtime_defaults
from .layered import (
    MISSING,
    ConfigEntry,
    ConfigSource,
    LayeredConfig,
    ScopedConfig,
    coerce_bool,
    coerce_float,
    coerce_int,
    coerce_list,
    coerce_path,
    coerce_str,
)
from .loaders import (
    flatten_mapping,
    load_config_file,
    load_dotenv,
    load_environment,
    normalize_env_key,
    unflatten_mapping,
)

__all__ = [
    "CORE_DEFAULTS",
    "MISSING",
    "ConfigEntry",
    "ConfigSource",
    "LayeredConfig",
    "ScopedConfig",
    "coerce_bool",
    "coerce_float",
    "coerce_int",
    "coerce_list",
    "coerce_path",
    "coerce_str",
    "default_for",
    "flatten_mapping",
    "load_config_file",
    "load_dotenv",
    "load_environment",
    "normalize_env_key",
    "unflatten_mapping",
    "with_runtime_defaults",
]
