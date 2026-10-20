"""Framework-wide constants: exit codes, status tags, key names, defaults."""

from __future__ import annotations

from enum import IntEnum
from typing import Final

PACKAGE_NAME: Final[str] = "korodexios-core"
PACKAGE_IMPORT_NAME: Final[str] = "korodexios_core"
MODULE_PACKAGE: Final[str] = "korodexios_core.modules"
MODULES_DIR_NAME: Final[str] = "modules"
MANIFEST_NAME: Final[str] = "korodex_module.toml"
ENV_PREFIX: Final[str] = "KORODEX"

CONFIG_CANDIDATES: Final[tuple[str, ...]] = ("korodex.yml", "korodex.yaml", "korodex.json")
ROOT_MARKER_FILES: Final[tuple[str, ...]] = (".korodex-root", *CONFIG_CANDIDATES)
ROOT_MARKER_PROJECTS: Final[tuple[str, ...]] = ("korodexios-core",)
DOTENV_NAME: Final[str] = ".env"

STATE_DIR_NAME: Final[str] = "var"
LOG_DIR_NAME: Final[str] = "var/log"
DATA_DIR_NAME: Final[str] = "var/lib"

ENCODING: Final[str] = "utf-8"
KEY_SEPARATOR: Final[str] = "."
ENV_NESTED_SEPARATOR: Final[str] = "__"
ENV_LIST_SEPARATOR: Final[str] = ","

HOME_ENV_VAR: Final[str] = "KORODEX_HOME"
CONFIG_ENV_VAR: Final[str] = "KORODEX_CONFIG"
ENV_FILE_ENV_VAR: Final[str] = "KORODEX_ENV_FILE"
TRACEBACK_ENV_VAR: Final[str] = "KORODEX_TRACEBACK"

TAG_OK: Final[str] = "[✓]"
TAG_ASK: Final[str] = "[?]"
TAG_ERROR: Final[str] = "[!]"
TAG_NOTE: Final[str] = "[i]"

SIGNAL_EXIT_BASE: Final[int] = 128
DEFAULT_STOP_TIMEOUT: Final[float] = 5.0
DEFAULT_TICK_INTERVAL: Final[float] = 1.0

LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ExitCode(IntEnum):
    """Process exit codes; ABORTED follows the shell convention 128 + signum."""

    SUCCESS = 0
    ERROR = 1
    USAGE_ERROR = 2
    CONFIG_ERROR = 3
    ABORTED = 130

    @classmethod
    def from_signal(cls, signum: int) -> int:
        """Map a signal number to the conventional shell exit code (128 + signum)."""
        return SIGNAL_EXIT_BASE + int(signum)
