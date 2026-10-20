"""Framework-wide constants: exit codes, status tags, key names, defaults."""

from __future__ import annotations

from enum import IntEnum
from typing import Final

PACKAGE_NAME: Final[str] = "korodexios-core"

DOCTOR_SCHEMA_VERSION: Final[int] = 1
"""Version of the payload kdx doctor --json emits; bump on any removal."""
IPC_SCHEMA_VERSION: Final[int] = 1
"""Version of the /api/v1/schema contract; bump on any removal or rename."""
PACKAGE_IMPORT_NAME: Final[str] = "korodexios_core"
MODULE_PACKAGE: Final[str] = "korodexios_core.modules"
MODULES_DIR_NAME: Final[str] = "modules"
MANIFEST_NAME: Final[str] = "kdx_module.toml"
ENV_PREFIX: Final[str] = "KDX"
ENV_PREFIX_ALIASES: Final[tuple[str, ...]] = ("KDX", "KORODEXIOS")

CONFIG_CANDIDATES: Final[tuple[str, ...]] = (
    "kdx.yml",
    "kdx.yaml",
    "kdx.json",
    "korodexios.yml",
    "korodexios.yaml",
    "korodexios.json",
)
LOCAL_CONFIG_CANDIDATES: Final[tuple[str, ...]] = (
    "kdx.local.yml",
    "kdx.local.yaml",
    "kdx.local.json",
    "korodexios.local.yml",
    "korodexios.local.yaml",
    "korodexios.local.json",
)
ROOT_MARKER_FILES: Final[tuple[str, ...]] = (
    ".kdx-root",
    ".korodexios-root",
    *CONFIG_CANDIDATES,
    *LOCAL_CONFIG_CANDIDATES,
)
ROOT_MARKER_PROJECTS: Final[tuple[str, ...]] = ("korodexios-core",)
DOTENV_NAME: Final[str] = ".env"

STATE_DIR_NAME: Final[str] = "var"
LOG_DIR_NAME: Final[str] = "var/log"
DATA_DIR_NAME: Final[str] = "var/lib"

ENCODING: Final[str] = "utf-8"
KEY_SEPARATOR: Final[str] = "."
ENV_NESTED_SEPARATOR: Final[str] = "__"
ENV_LIST_SEPARATOR: Final[str] = ","

HOME_ENV_VAR: Final[str] = "KDX_HOME"
CONFIG_ENV_VAR: Final[str] = "KDX_CONFIG"
ENV_FILE_ENV_VAR: Final[str] = "KDX_ENV_FILE"
TRACEBACK_ENV_VAR: Final[str] = "KDX_TRACEBACK"
HOME_ENV_VARS: Final[tuple[str, ...]] = ("KDX_HOME", "KORODEXIOS_HOME")
CONFIG_ENV_VARS: Final[tuple[str, ...]] = ("KDX_CONFIG", "KORODEXIOS_CONFIG")
ENV_FILE_ENV_VARS: Final[tuple[str, ...]] = (
    "KDX_ENV_FILE",
    "KORODEXIOS_ENV_FILE",
)
TRACEBACK_ENV_VARS: Final[tuple[str, ...]] = (
    "KDX_TRACEBACK",
    "KORODEXIOS_TRACEBACK",
)

TAG_OK: Final[str] = "[✓]"
TAG_ASK: Final[str] = "[?]"
TAG_ERROR: Final[str] = "[!]"
TAG_NOTE: Final[str] = "[i]"

TAG_ALIASES: Final[dict[str, str]] = {
    TAG_OK: "[OK]",
    TAG_ASK: "[?]",
    TAG_ERROR: "[!]",
    TAG_NOTE: "[i]",
}


def tags_for(encoding: str | None) -> dict[str, str]:
    """Return the ASCII tag aliases a stream with *encoding* needs, if any.

    Legacy Windows consoles (CP1250/CP1252) cannot encode the check mark, and a
    UnicodeEncodeError in the middle of a friendly CLI is unacceptable, so the
    renderers swap in ASCII tags when the stream cannot take the unicode ones.
    """
    if not encoding:
        return {}
    try:
        TAG_OK.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return dict(TAG_ALIASES)
    return {}


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
