"""Configuration layer: strict precedence across CLI, environment, file, defaults."""

from __future__ import annotations

from .defaults import CORE_DEFAULTS, default_for, with_runtime_defaults
from .keys import (
    MODULE_SCOPE,
    PROJECT_SCOPE,
    core_keys,
    is_core_key,
    is_known_key,
    is_project_key,
    module_of,
    module_scope,
    project_key_names,
    suggest,
    unknown_keys,
)
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
    "MODULE_SCOPE",
    "PROJECT_SCOPE",
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
    "core_keys",
    "default_for",
    "flatten_mapping",
    "is_core_key",
    "is_known_key",
    "is_project_key",
    "load_config_file",
    "load_dotenv",
    "load_environment",
    "module_of",
    "module_scope",
    "normalize_env_key",
    "project_key_names",
    "suggest",
    "unflatten_mapping",
    "unknown_keys",
    "with_runtime_defaults",
]
