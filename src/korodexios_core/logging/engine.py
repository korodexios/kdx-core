"""Structured logging engine: diagnostics on stderr, clean stdout for the UI."""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import ENCODING, LOG_LEVELS
from ..exceptions import ChassisError, PathDiscoveryError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config.layered import LayeredConfig
    from ..paths import AppPaths

_LEVEL_ALIASES: dict[str, int] = {
    "NOTSET": logging.NOTSET,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_ANSI: dict[str, str] = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "cyan": "\x1b[36m",
}

_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "blue",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold",
}


def resolve_level(value: int | str) -> int:
    """Translate a level name or number into a stdlib logging level."""
    if isinstance(value, bool):
        raise ChassisError(
            f"unknown log level {value!r}",
            hint="Use one of: " + ", ".join(LOG_LEVELS) + ".",
        )
    if isinstance(value, int):
        return value
    token = str(value).strip().upper()
    if token in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[token]
    raise ChassisError(
        f"unknown log level {value!r}",
        hint="Use one of: " + ", ".join(LOG_LEVELS) + ".",
    )


def _shorten(value: Any, limit: int = 120) -> str:
    text = text_value(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}..."


def text_value(value: Any) -> str:
    """Render *value* for the human-readable log format."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{key}: {_shorten(item, 40)}" for key, item in value.items()) + "}"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "[" + ", ".join(_shorten(item, 40) for item in value) + "]"
    return str(value)


def _stamp(record: logging.LogRecord, *, utc: bool) -> str:
    if utc:
        moment = datetime.fromtimestamp(record.created, tz=UTC)
    else:
        moment = datetime.fromtimestamp(record.created)
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')}.{int(record.msecs):03d}"


def _fields_of(record: logging.LogRecord) -> Mapping[str, Any]:
    fields = getattr(record, "korodex_fields", None)
    if isinstance(fields, Mapping):
        return fields
    return {}


class TextFormatter(logging.Formatter):
    """Compact, optionally coloured human format for terminal diagnostics."""

    def __init__(self, *, color: bool = False, utc: bool = False) -> None:
        super().__init__()
        self._color = color
        self._utc = utc

    def format(self, record: logging.LogRecord) -> str:
        """Render one record, appending structured fields and tracebacks."""
        level = record.levelname
        fields = _fields_of(record)
        suffix = ""
        if fields:
            suffix = "  " + " ".join(f"{key}={_shorten(value)}" for key, value in fields.items())
        head = f"{_stamp(record, utc=self._utc)} [{level:<7}] {record.name}: {record.getMessage()}"
        if self._color:
            token = _ANSI.get(_LEVEL_COLORS.get(level, "reset"), "")
            head = head.replace(f"[{level:<7}]", f"{token}[{level:<7}]{_ANSI['reset']}", 1)
        body = f"{head}{suffix}"
        if record.exc_info:
            body = f"{body}\n{self.formatException(record.exc_info)}"
        return body


class JsonFormatter(logging.Formatter):
    """One JSON object per line, safe for log shippers and CI artefacts."""

    def __init__(self, *, utc: bool = False) -> None:
        super().__init__()
        self._utc = utc

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a single JSON line."""
        if self._utc:
            moment = datetime.fromtimestamp(record.created, tz=UTC)
        else:
            moment = datetime.fromtimestamp(record.created)
        payload: dict[str, Any] = {
            "ts": moment.isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = _fields_of(record)
        if fields:
            payload["fields"] = {str(key): _shorten(value) for key, value in fields.items()}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class KorodexLogger:
    """Thin wrapper adding structured fields and child namespaces."""

    def __init__(self, logger: logging.Logger, *, fields: Mapping[str, Any] | None = None) -> None:
        self._logger = logger
        self._fields: dict[str, Any] = dict(fields or {})

    @property
    def name(self) -> str:
        """Return the underlying logger name."""
        return self._logger.name

    @property
    def adapter(self) -> logging.Logger:
        """Return the stdlib logger for advanced integrations."""
        return self._logger

    def bind(self, **fields: Any) -> KorodexLogger:
        """Return a child logger that always logs *fields*."""
        merged = dict(self._fields)
        merged.update(fields)
        return KorodexLogger(self._logger, fields=merged)

    def child(self, name: str) -> KorodexLogger:
        """Return a logger namespaced below this one (for example 'korodex.backup')."""
        return KorodexLogger(
            logging.getLogger(f"{self._logger.name}.{name}"),
            fields=self._fields,
        )

    def log(self, level: int, message: str, **fields: Any) -> None:
        """Emit *message* at *level* with per-call structured *fields*."""
        merged = dict(self._fields)
        merged.update(fields)
        self._logger.log(level, message, extra={"korodex_fields": merged})

    def debug(self, message: str, **fields: Any) -> None:
        """Emit a debug diagnostic."""
        self.log(logging.DEBUG, message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        """Emit an informational diagnostic."""
        self.log(logging.INFO, message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        """Emit a warning diagnostic."""
        self.log(logging.WARNING, message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        """Emit an error diagnostic."""
        self.log(logging.ERROR, message, **fields)

    def critical(self, message: str, **fields: Any) -> None:
        """Emit a critical diagnostic."""
        self.log(logging.CRITICAL, message, **fields)

    def exception(self, message: str, **fields: Any) -> None:
        """Emit an error diagnostic with the active exception attached."""
        merged = dict(self._fields)
        merged.update(fields)
        self._logger.error(message, exc_info=True, extra={"korodex_fields": merged})

    def flush(self) -> None:
        """Flush every handler attached to the underlying logger."""
        for handler in self._logger.handlers:
            try:
                handler.flush()
            except (OSError, ValueError):
                continue

    def __repr__(self) -> str:
        return f"KorodexLogger(name={self._logger.name!r}, fields={sorted(self._fields)})"


class LoggerFactory:
    """Factory handing out namespaced loggers that share one chassis pipeline."""

    def __init__(
        self,
        root_logger: logging.Logger,
        *,
        level: int,
        json_output: bool,
        color: bool,
        utc: bool,
        log_file: Path | None = None,
    ) -> None:
        self._root = root_logger
        self._level = level
        self._json_output = json_output
        self._color = color
        self._utc = utc
        self._log_file = log_file

    @property
    def root_logger(self) -> logging.Logger:
        """Return the root chassis logger."""
        return self._root

    @property
    def level(self) -> int:
        """Return the effective level."""
        return self._level

    @property
    def log_file(self) -> Path | None:
        """Return the active log file, when file logging is enabled."""
        return self._log_file

    def get(self, name: str) -> KorodexLogger:
        """Return a logger for *name* under the chassis namespace."""
        clean = str(name).strip(".")
        if not clean or clean == self._root.name:
            return KorodexLogger(self._root)
        return KorodexLogger(logging.getLogger(f"{self._root.name}.{clean}"))

    def stats(self) -> dict[str, Any]:
        """Return a JSON-friendly description of the logging pipeline."""
        return {
            "logger": self._root.name,
            "level": logging.getLevelName(self._level),
            "handlers": [type(handler).__name__ for handler in self._root.handlers],
            "format": "json" if self._json_output else "text",
            "color": self._color,
            "utc": self._utc,
            "log_file": str(self._log_file) if self._log_file else None,
        }


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (ValueError, OSError):
        return False


def ensure_utf8_streams() -> dict[str, str]:
    """Force UTF-8 on stdout/stderr where the platform allows it."""
    report: dict[str, str] = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            report[name] = "absent"
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding=ENCODING, errors="backslashreplace")
                report[name] = ENCODING
            except (ValueError, OSError):
                report[name] = getattr(stream, "encoding", "unknown")
        else:
            report[name] = str(getattr(stream, "encoding", "unknown"))
    return report


def build_logger(
    config: LayeredConfig,
    paths: AppPaths,
    *,
    name: str = "korodex",
) -> tuple[logging.Logger, LoggerFactory]:
    """Build the chassis logger from configuration and resolved paths."""
    level = resolve_level(config.get("core.log.level", "INFO"))
    json_output = config.get_bool("core.log.json", False)
    utc = config.get_bool("core.log.utc", False)
    want_color = config.get_bool("core.log.color", True)
    color = want_color and _is_tty(sys.stderr) and not json_output

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(
        JsonFormatter(utc=utc) if json_output else TextFormatter(color=color, utc=utc)
    )
    logger.addHandler(stream_handler)

    log_file: Path | None = None
    if config.get_bool("core.log.file_enabled", True):
        try:
            paths.ensure_directories()
            log_file = paths.log_dir / str(config.get("core.log.file", "korodex.log"))
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=config.get_int("core.log.max_bytes", 1_048_576),
                backupCount=config.get_int("core.log.backup_count", 5),
                encoding=ENCODING,
                delay=True,
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(
                JsonFormatter(utc=utc) if json_output else TextFormatter(color=False, utc=utc)
            )
            logger.addHandler(file_handler)
        except (OSError, PathDiscoveryError) as exc:
            logger.warning(
                "file logging disabled; continuing with stderr only",
                extra={
                    "korodex_fields": {"path": str(log_file or paths.log_dir), "error": str(exc)}
                },
            )
            log_file = None

    factory = LoggerFactory(
        logger,
        level=level,
        json_output=json_output,
        color=color,
        utc=utc,
        log_file=log_file,
    )
    return logger, factory


def close_logger(logger: logging.Logger) -> None:
    """Flush and detach every handler owned by *logger*."""
    for handler in list(logger.handlers):
        try:
            handler.flush()
        except (OSError, ValueError):
            continue
        try:
            handler.close()
        except OSError:
            continue
        logger.removeHandler(handler)


DEFAULT_TAIL_LINES = 50
DEFAULT_POLL_INTERVAL = 0.5
_TAIL_CHUNK = 8192


def tail_lines(path: Path, count: int = DEFAULT_TAIL_LINES) -> list[str]:
    """Return the last *count* lines of *path* without reading the whole file.

    A missing or unreadable file yields an empty list: the CLI decides what the
    operator deserves to hear, the helper never raises.
    """
    if count <= 0:
        return []
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    wanted = int(count)
    with target.open("rb") as handle:
        position = size
        chunks: list[bytes] = []
        seen = 0
        while position > 0 and seen <= wanted:
            step = min(_TAIL_CHUNK, position)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            chunks.append(block)
            seen += block.count(b"\n")
            if position == 0 and seen > wanted:
                break
        blob = b"".join(reversed(chunks))
    decoded = blob.decode(ENCODING, errors="replace")
    lines = decoded.splitlines()
    return lines[-wanted:]


def truncate_log(path: Path) -> int:
    """Empty *path* in place (same inode) and return the bytes removed."""
    target = Path(path)
    try:
        before = target.stat().st_size
    except OSError:
        return 0
    with target.open("w", encoding=ENCODING) as handle:
        handle.write("")
    return int(before)


class LogTailer:
    """Follow an appending log file, tolerating truncation and rotation.

    Polling is deliberate: it is identical on Linux, macOS, and Windows, needs no
    extra dependency, and a 0.5 s cadence is invisible to an operator reading logs.
    """

    def __init__(self, path: Path, *, poll_interval: float = DEFAULT_POLL_INTERVAL) -> None:
        self.path = Path(path)
        self.poll_interval = max(0.05, float(poll_interval))
        self._position = 0
        self._inode: int | None = None
        self._partial = ""

    def seek_end(self) -> None:
        """Start following from the end of the current file."""
        self._partial = ""
        with contextlib.suppress(OSError):
            stat = self.path.stat()
            self._position = int(stat.st_size)
            self._inode = int(stat.st_ino)

    def read_new(self) -> list[str]:
        """Return complete lines appended since the last call."""
        try:
            stat = self.path.stat()
        except OSError:
            return []
        size = int(stat.st_size)
        inode = int(stat.st_ino)
        if self._inode is not None and inode != self._inode:
            self._inode = inode
            self._position = 0
            self._partial = ""
        elif size < self._position:
            self._position = 0
            self._partial = ""
        if size <= self._position:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self._position)
            blob = handle.read(size - self._position)
        self._position = size
        self._inode = inode
        text = self._partial + blob.decode(ENCODING, errors="replace")
        if not text.endswith("\n"):
            head, _, tail = text.rpartition("\n")
            if not head:
                self._partial = text
                return []
            self._partial = tail
            text = head
        else:
            self._partial = ""
        return text.splitlines()

    def wait(self) -> None:
        """Sleep one polling interval."""
        time.sleep(self.poll_interval)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for status output."""
        return {
            "path": str(self.path),
            "position": self._position,
            "poll_interval": self.poll_interval,
            "exists": self.path.is_file(),
        }
