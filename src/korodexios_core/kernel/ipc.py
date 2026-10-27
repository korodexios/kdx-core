"""Local HTTP control plane: health probe, status snapshot, remote shutdown.

The chassis is headless-first, so a running daemon must be observable from the
outside without a heavyweight dependency. A stdlib ThreadingHTTPServer gives the
CLI, curl, Docker HEALTHCHECK, and future web dashboards the same JSON on Linux,
macOS, and Windows. It binds loopback by default and any host on request: a
homelab, a VPN peer, or a LAN dashboard can query a remote chassis directly,
with `core.ipc.auth` deciding whether a token is required to read status.
"""

from __future__ import annotations

import contextlib
import errno
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from ..constants import IPC_SCHEMA_VERSION
from ..units import page_size
from ..version import CORE_API_VERSION
from .instance_lock import pid_alive

DISCOVERY_NAME = "chassis.ipc"
TOKEN_HEADER = "X-Kdx-Token"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8123
DEFAULT_TOKEN_BYTES = 16
WILDCARD_HOSTS = ("0.0.0.0", "::")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_SEGMENT_LENGTH: Final[int] = 255
"""Longest single path segment a static mount will look at."""

MAX_IPC_PAYLOAD_BYTES: Final[int] = 10 * 1024 * 1024
"""Largest request body the control plane will buffer, in bytes (10 MiB).

``_read_payload`` reads the ``Content-Length`` body straight into memory, so an
untrusted or malformed request that announces a huge body would exhaust the
daemon memory before the handler ever ran. A body over this limit is rejected
with HTTP 413 before a single byte is buffered, so the control plane answers
instead of dying.
"""

PAYLOAD_TOO_LARGE: Final[int] = 413
"""The status the control plane answers when a request body exceeds the limit."""

WINDOWS_DEVICE_NAMES: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
"""Names Windows reserves with or without an extension, so a mount rejects them."""

CASE_INSENSITIVE_PATHS: Final[bool] = os.name == "nt"
"""Whether this platform compares path case the way Windows does."""

IS_LINUX: Final[bool] = sys.platform.startswith("linux")
"""Cached once: ru_maxrss is already in bytes on Linux, in KiB everywhere else."""

HEALTH_PATH = "/health"
STATUS_PATH = "/api/v1/status"
SHUTDOWN_PATH = "/api/v1/shutdown"
SCHEMA_PATH = "/api/v1/schema"
RPC_PATH = "/api/v1/rpc"
RELOAD_PATH = "/api/v1/reload"
ROUTES = (
    HEALTH_PATH,
    STATUS_PATH,
    SHUTDOWN_PATH,
    SCHEMA_PATH,
    RPC_PATH,
    RELOAD_PATH,
)
RPC_METHODS = ("POST",)
RELOAD_METHODS = ("POST",)

HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
)
"""The verbs a declarative route may answer.

One server dispatches this vocabulary and nothing else, and the chassis' own CLI
client only ever speaks it. A route that declares 'FETCH', a blank entry, or a 'GET'
with a CR and LF welded on the end smuggles something the request line cannot carry,
so the chassis refuses the declaration at registration instead of answering an
ambiguous request later."""

DEFAULT_ROUTE_METHODS: Final[tuple[str, ...]] = ("GET",)
"""The verb a route answers when it declares no methods tuple of its own."""

ROUTE_PATH_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[\s\x00-\x20\x7f]")
"""Characters a route path may never carry: whitespace, NUL, the other C0 controls,
and DEL. A CR or LF inside a path is a header-splitting hazard on the way out, and a
space makes the request line ambiguous for anything sitting in front of the daemon."""

CONTENT_JSON = "application/json"
CONTENT_OCTET_STREAM = "application/octet-stream"
CONTENT_PLAIN = "text/plain; charset=utf-8"
CONTENT_HTML = "text/html; charset=utf-8"
STATIC_METHODS = ("GET", "HEAD")
"""Content types the control plane can answer with.

A dashboard is the reason these exist: a module that renders HTML, serves a
stylesheet, or hands back a PNG wants its own type on the wire, not JSON holding a
string. The chassis still owns the socket; a module only decides what its body
means, and these names are the vocabulary it may choose from.
"""

_EXTRA_CONTENT_TYPES: dict[str, str] = {
    ".css": "text/css",
    ".csv": "text/csv",
    ".htm": "text/html",
    ".html": "text/html",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".wasm": "application/wasm",
}


def guess_content_type(name: str) -> str:
    """Return the media type for *name*, defaulting to octet-stream.

    The small table settles the extensions mimetypes answers differently per
    platform - '.js' is the usual offender, and on Windows '.csv' comes back as
    'application/vnd.ms-excel' - so a dashboard gets the same type on every host,
    and a static build keeps caching it.
    """
    suffix = Path(str(name)).suffix.lower()
    if suffix in _EXTRA_CONTENT_TYPES:
        return _EXTRA_CONTENT_TYPES[suffix]
    guessed, _encoding = mimetypes.guess_type(str(name))
    return guessed or CONTENT_OCTET_STREAM


def normalize_route_path(path: str) -> str:
    """Return *path* without a query string and with one trailing slash at most."""
    clean = str(path).split("?", 1)[0].strip()
    if not clean.startswith("/"):
        clean = "/" + clean
    return clean.rstrip("/") or "/"


@dataclass(frozen=True, slots=True)
class IpcRequest:
    """What a module route handler receives from the control plane."""

    method: str
    path: str
    query: Mapping[str, list[str]]
    payload: Any = None
    headers: Mapping[str, str] | None = None

    def argument(self, name: str, default: Any = None) -> Any:
        """Return one query argument, then a body field, then *default*."""
        values = self.query.get(name)
        if values:
            return values[0]
        if isinstance(self.payload, Mapping):
            return self.payload.get(name, default)
        return default


@dataclass(frozen=True, slots=True)
class IpcRoute:
    """One HTTP route a module contributes to the chassis control plane.

    The chassis owns the listener; a module declares a path, a handler, and
    whether the shared token is required. Handlers return either a JSON-friendly
    payload (served with 200) or a (status, payload) pair.

    The declaration is checked here, at registration time: a path carrying
    whitespace, a CR, an LF, or any other control character is refused, as is a verb
    outside the standard set, so a malformed route never reaches the socket to make
    the daemon answer a request line it cannot phrase back cleanly.
    """

    path: str
    handler: Callable[[IpcRequest], Any]
    methods: tuple[str, ...] = ("GET",)
    auth: bool = True
    module: str = ""
    summary: str = ""
    content_type: str = CONTENT_JSON

    def __post_init__(self) -> None:
        raw = str(self.path)
        illegal = ROUTE_PATH_UNSAFE.search(raw)
        if illegal is not None:
            raise IpcError(
                f"route path {raw!r} carries an illegal character {illegal.group(0)!r}",
                hint="A route path is a URL path: no whitespace, CR, LF, NUL, or DEL.",
            )
        clean = normalize_route_path(raw)
        if clean in ROUTES:
            raise IpcError(
                f"route {self.path!r} is reserved by the chassis control plane",
                hint="Pick a path under your module name, for example '/api/v1/metrics'.",
            )
        object.__setattr__(self, "path", clean)
        verbs: list[str] = []
        for method in self.methods:
            token = str(method).strip().upper()
            if not token:
                raise IpcError(
                    f"route {clean} declares an empty HTTP method",
                    hint="Declare methods like ('GET', 'POST'), or () for GET alone.",
                )
            if token not in HTTP_METHODS:
                raise IpcError(
                    f"route {clean} declares HTTP method {token!r}, which one server cannot answer",
                    hint="Recognized methods: GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS.",
                )
            if token not in verbs:
                verbs.append(token)
        object.__setattr__(self, "methods", tuple(verbs) or DEFAULT_ROUTE_METHODS)

    def allows(self, method: str) -> bool:
        """Return True when this route answers *method*."""
        return str(method).upper() in self.methods

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the route."""
        return {
            "path": self.path,
            "methods": list(self.methods),
            "auth": bool(self.auth),
            "module": self.module,
            "summary": self.summary,
            "content_type": self.content_type,
        }

    def declared_content_type(self) -> str:
        """Return the media type the route author chose, defaulting to JSON."""
        return str(self.content_type or CONTENT_JSON).strip() or CONTENT_JSON

    def binary_content_type(self) -> str:
        """Return the media type used when a handler answers with raw bytes.

        A route that declared a content type keeps it. One that did not gets its
        own extension read - '/assets/logo.png' answers as an image - and a route
        with neither falls back to octet-stream.
        """
        declared = self.declared_content_type()
        if declared != CONTENT_JSON:
            return declared
        if Path(self.path).suffix:
            return guess_content_type(self.path)
        return CONTENT_OCTET_STREAM


@dataclass(frozen=True, slots=True)
class IpcResponse:
    """A control-plane body that carries its own meaning: HTML, CSS, JS, bytes.

    A module route returns this instead of a JSON-friendly payload when the bytes
    are the answer. Nothing here is dashboard-specific: the chassis writes what it
    is handed, and the media type is the module's to choose.
    """

    body: bytes = b""
    content_type: str = CONTENT_JSON
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # Declared as bytes; a caller may hand str, bytearray, or memoryview and
        # gets bytes back, so the checks read the value through an Any view.
        raw: Any = self.body
        if isinstance(raw, str):
            object.__setattr__(self, "body", raw.encode("utf-8"))
        elif isinstance(raw, bytearray | memoryview):
            object.__setattr__(self, "body", bytes(raw))
        elif not isinstance(raw, bytes):
            raise IpcError(
                f"IpcResponse body must be bytes or str, got {type(raw).__name__}",
                hint="return IpcResponse.text() for text and IpcResponse(body=bytes) for data",
            )
        status = int(self.status)
        if not 100 <= status <= 599:
            raise IpcError(f"IpcResponse status {status} is outside 100..599")
        object.__setattr__(self, "status", status)
        if not str(self.content_type or "").strip():
            object.__setattr__(self, "content_type", CONTENT_JSON)

    @property
    def length(self) -> int:
        """Return the number of bytes on the wire."""
        return len(self.body)

    @property
    def is_text(self) -> bool:
        """Return True when the media type is a text-shaped one."""
        kind = str(self.content_type).split(";", 1)[0].strip().lower()
        return kind.startswith("text/") or kind in {"application/json", "application/javascript"}

    def header(self, name: str) -> str:
        """Return the response header *name*, or an empty string."""
        for key, value in self.headers:
            if str(key).lower() == str(name).lower():
                return str(value)
        return ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of this response."""
        return {
            "status": self.status,
            "content_type": self.content_type,
            "length": self.length,
            "headers": {str(key): str(value) for key, value in self.headers},
        }

    @classmethod
    def text(
        cls,
        text: str,
        *,
        content_type: str = CONTENT_PLAIN,
        status: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> IpcResponse:
        """Return a UTF-8 text response."""
        return cls(text.encode("utf-8"), content_type, status, headers)

    @classmethod
    def html(
        cls,
        markup: str,
        *,
        status: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> IpcResponse:
        """Return an HTML response - a dashboard's front door."""
        return cls(markup.encode("utf-8"), CONTENT_HTML, status, headers)

    @classmethod
    def file(
        cls,
        path: str | Path,
        *,
        content_type: str | None = None,
        status: int = 200,
        cache_control: str = "no-cache",
    ) -> IpcResponse:
        """Return the contents of *path* on disk, labelled with its own type."""
        target = Path(path)
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise IpcError(
                f"cannot read {target}: {exc}", hint="serve a file that exists on this host"
            ) from exc
        headers: list[tuple[str, str]] = []
        if cache_control:
            headers.append(("Cache-Control", cache_control))
        return cls(raw, content_type or guess_content_type(target.name), status, tuple(headers))


@dataclass(frozen=True, slots=True)
class IpcStaticMount:
    """One directory a module serves under a route prefix.

    'register_static_dir("/ui", ui_dir)' is the whole contract: the prefix is the
    URL space, the directory is the file space, and an index name is what '/'
    means. Nothing below the prefix may climb out of the directory, so a crafted
    '..' reads nothing the module did not publish.
    """

    route_prefix: str
    directory: Path
    index_names: tuple[str, ...] = ("index.html",)
    cache_control: str = "no-cache"
    auth: bool = False
    module: str = ""

    def __post_init__(self) -> None:
        prefix = normalize_route_path(self.route_prefix).rstrip("/")
        if not prefix or prefix == "/":
            raise IpcError(
                "a static mount needs a route prefix below '/', for example '/ui'",
                hint="a mount at '/' would swallow every control-plane route",
            )
        if prefix in ROUTES:
            raise IpcError(
                f"route prefix {self.route_prefix!r} is reserved by the chassis control plane",
                hint="pick a prefix under your module name, for example '/metrics'",
            )
        root = Path(self.directory).expanduser()
        if not root.is_dir():
            raise IpcError(
                f"static directory {root} does not exist",
                hint="create the directory before registering the mount",
            )
        object.__setattr__(self, "route_prefix", prefix)
        object.__setattr__(self, "directory", root)
        if not tuple(self.index_names):
            object.__setattr__(self, "index_names", ("index.html",))

    @property
    def prefix(self) -> str:
        """Return the normalized URL prefix, without a trailing slash."""
        return str(self.route_prefix)

    def allows(self, method: str) -> bool:
        """Return True when this mount answers *method*."""
        return str(method).upper() in STATIC_METHODS

    def matches(self, path: str) -> bool:
        """Return True when *path* is the prefix or lives below it."""
        clean = normalize_route_path(path)
        return clean == self.prefix or clean.startswith(self.prefix + "/")

    def relative(self, path: str) -> str:
        """Return the path below the prefix, without a leading slash."""
        clean = normalize_route_path(path)
        if clean == self.prefix:
            return ""
        remainder: str = clean[len(self.prefix) :]
        return remainder.lstrip("/")

    def resolve(self, subpath: str) -> Path | None:
        """Return the file *subpath* points at inside the directory, or None.

        The guard is the point: the subpath is unquoted first, because a client's
        %5c is a separator and not a filename; both separators are folded to one;
        every segment is checked, including the Windows names a POSIX filesystem
        happily stores (a drive letter, an alternate data stream after a colon, a
        reserved device name); the result is resolved; and a target that lands
        outside the mounted directory is refused. That is what turns
        '/../../etc/passwd', '..%5c..%5cwindows', and 'report.txt:stream' into a
        refusal instead of a leak.
        """
        text = urllib.parse.unquote(str(subpath)).replace("\\", "/").strip("/")
        if "\x00" in text:
            return None
        if not text:
            root_dir = Path(self.directory)
            return root_dir if root_dir.is_dir() else None
        parts = PurePosixPath(text).parts
        for part in parts:
            if (
                part in {"", ".", ".."}
                or part.startswith("/")
                or len(part) > MAX_SEGMENT_LENGTH
                or any(character in part for character in ":|")
                or _windows_device_name(part)
            ):
                return None
        root = Path(self.directory).resolve()
        candidate = (root / Path(*parts)).resolve()
        return candidate if _inside_directory(candidate, root) else None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the mount."""
        return {
            "route_prefix": self.prefix,
            "directory": str(self.directory),
            "index_names": list(self.index_names),
            "cache_control": self.cache_control,
            "auth": bool(self.auth),
            "module": self.module,
            "methods": list(STATIC_METHODS),
        }


def _windows_device_name(part: str) -> bool:
    """Return True when *part* is a name Windows reserves, extension or not."""
    stem = str(part).split(".", 1)[0].strip().lower()
    return stem in WINDOWS_DEVICE_NAMES


def _inside_directory(candidate: Path, root: Path) -> bool:
    """Return True when *candidate* stayed inside *root* after resolution.

    A case-insensitive filesystem reports the same file under two spellings, so a
    case fold here keeps a legitimate request from turning into a spurious 404.
    """
    if candidate.is_relative_to(root):
        return True
    if not CASE_INSENSITIVE_PATHS:
        return False
    folded = PurePosixPath(candidate.as_posix().casefold())
    return folded.is_relative_to(PurePosixPath(root.as_posix().casefold()))


def new_token() -> str:
    """Return a fresh ephemeral shared secret."""
    return secrets.token_hex(DEFAULT_TOKEN_BYTES)


def is_wildcard_host(host: str) -> bool:
    """Return True when the bind address accepts every interface."""
    return str(host).strip() in WILDCARD_HOSTS


def is_loopback_host(host: str) -> bool:
    """Return True when only this machine can reach the bind address."""
    return str(host).strip() in LOOPBACK_HOSTS


class IpcError(ValueError):
    """Raised when the control plane cannot be started or reached."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


if sys.platform == "win32":  # pragma: no cover - Windows only
    def _ru_maxrss() -> int | None:
        """Return peak resident memory; Windows answers no ru_maxrss to a caller."""
        return None

else:
    import resource

    def _ru_maxrss() -> int | None:
        """Return peak resident memory in the unit this platform reports it in."""
        try:
            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        except (OSError, ValueError):
            return None


def process_memory() -> dict[str, Any]:
    """Return this process's resident memory, best effort, cross-platform."""
    result: dict[str, Any] = {"rss_bytes": None, "rss": "-", "source": "unavailable"}
    statm = Path("/proc/self/statm")
    try:
        if statm.is_file():
            resident_pages = int(statm.read_text(encoding="utf-8").split()[1])
            rss = max(0, resident_pages) * page_size()
            result.update(rss_bytes=rss, rss=_human_bytes(rss), source="proc/statm")
            return result
    except (OSError, ValueError, IndexError):
        return result
    usage = _ru_maxrss()
    if usage is not None:
        scale = 1 if IS_LINUX else 1024
        rss = int(usage) * scale
        result.update(rss_bytes=rss, rss=_human_bytes(rss), source="getrusage")
    return result


def sys_is_linux() -> bool:
    """Return True when ru_maxrss is already reported in bytes (Linux).

    Answered from a constant computed once at import: a status snapshot or a
    heartbeat tick must not pay for importing and calling :mod:'platform'.
    """
    return IS_LINUX


def _human_bytes(value: float) -> str:
    from ..units import format_bytes

    return format_bytes(int(value))


@dataclass(slots=True)
class IpcDiscovery:
    """What a client needs to reach a running chassis, as stored on disk."""

    pid: int
    host: str
    port: int
    token: str
    started_at: float
    path: Path | None = None
    scheme: str = "http"

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON shape written to the discovery file."""
        return {
            "pid": self.pid,
            "port": self.port,
            "host": self.host,
            "token": self.token,
            "started_at": self.started_at,
        }

    def base_url(self) -> str:
        """Return the base URL for this endpoint."""
        return f"{self.scheme}://{self.host}:{self.port}"

    @classmethod
    def remote(
        cls,
        target: str,
        *,
        default_port: int = DEFAULT_PORT,
        token: str = "",
    ) -> IpcDiscovery:
        """Build a client-side endpoint from 'http://host:port', 'host:port', or 'host'.

        Used by 'kdx status --target' so a LAN or VPN peer needs no local
        discovery file: the caller supplies the host and, when auth is on, the token.
        """
        text = str(target).strip()
        if not text:
            raise IpcError("no endpoint given", hint="pass --target http://host:port")
        if "://" not in text:
            text = "http://" + text
        parts = urllib.parse.urlsplit(text)
        try:
            port = int(parts.port) if parts.port else int(default_port)
        except ValueError as exc:
            raise IpcError(
                f"cannot read a port from {target!r}",
                hint="pass --target http://host:port",
            ) from exc
        if not parts.hostname:
            raise IpcError(
                f"cannot read a host from {target!r}", hint="pass --target http://host:port"
            )
        return cls(
            pid=0,
            host=str(parts.hostname),
            port=port,
            token=str(token),
            started_at=0.0,
            path=None,
            scheme=parts.scheme or "http",
        )

    def is_process_alive(self) -> bool:
        """Return True when the recorded pid still exists.

        Delegates to the shared :func:`pid_alive` so the discovery file and the
        instance lock answer the same question on every platform - on Windows a
        bogus pid must read as dead, or a stale discovery file looks like a live
        daemon and 'kdx status' hangs trying to reach it.
        """
        return pid_alive(self.pid)

    def request(
        self,
        route: str = STATUS_PATH,
        *,
        method: str = "GET",
        timeout: float = 2.0,
        payload: Any = None,
    ) -> tuple[int, Any]:
        """Call one route with the shared token; return (status, decoded body).

        A *payload* is sent as a JSON body, which is how a one-shot client
        delegates work to a daemon that is already running.
        """
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.token:
            headers[TOKEN_HEADER] = self.token
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url() + route,
            method=method,
            headers=headers,
            data=body,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), _decode(response.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), _decode(exc.read())

    def call(
        self,
        command: str,
        args: Mapping[str, Any] | None = None,
        *,
        timeout: float = 2.0,
        route: str = RPC_PATH,
    ) -> tuple[int, Any]:
        """Delegate one command to the running daemon; return (status, body)."""
        return self.request(
            route,
            method="POST",
            timeout=timeout,
            payload={"command": command, "args": dict(args or {})},
        )

    @classmethod
    def load(cls, path: Path) -> IpcDiscovery | None:
        """Read a discovery file; return None when it is missing or unreadable."""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                pid=int(raw.get("pid", 0)),
                host=str(raw.get("host", DEFAULT_HOST)),
                port=int(raw.get("port", 0)),
                token=str(raw.get("token", "")),
                started_at=float(raw.get("started_at", 0.0)),
                path=Path(path),
            )
        except (TypeError, ValueError):
            return None


def _index_file(mount: IpcStaticMount, directory: Path) -> Path | None:
    """Return the first index file *mount* accepts inside *directory*, or None."""
    for name in mount.index_names:
        candidate = directory / str(name)
        if candidate.is_file():
            return candidate
    return None


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


@dataclass(slots=True)
class IpcOptions:
    """Where to bind and where to announce the endpoint."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    discovery_path: Path | None = None
    token: str = field(default_factory=new_token)
    token_source: str = "ephemeral"
    auth: bool = True
    allow_anonymous_shutdown: bool = False
    fallback_attempts: int = 5


class _ControlServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that knows which chassis control plane it serves."""

    kdx_ipc: IpcServer


_TEARDOWN_ERRNOS: Final[frozenset[int]] = frozenset(
    {errno.EBADF, errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED}
)


def _is_teardown_error(exc: BaseException) -> bool:
    """Return True when *exc* is a socket that closed underneath a live handler.

    Stopping the control plane closes the listening socket while a worker thread is still
    answering a client: the peer goes away, the descriptor goes away, and the handler is
    left holding a write nobody will read. That is an ending, not a fault.
    """
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return isinstance(exc, OSError) and int(getattr(exc, "errno", 0) or 0) in _TEARDOWN_ERRNOS


class _ControlHandler(BaseHTTPRequestHandler):
    """Routes the chassis control plane, the module routes, and the token check."""

    protocol_version = "HTTP/1.1"
    server_version = "korodexios"

    def do_HEAD(self) -> None:
        """Answer a header-only read; static assets and caches rely on it."""
        self._dispatch("HEAD")

    def do_GET(self) -> None:
        """Serve /health, /api/v1/schema, /api/v1/status, and module GET routes."""
        self._dispatch("GET")

    def do_POST(self) -> None:
        """Serve /api/v1/shutdown, /api/v1/reload, /api/v1/rpc, and module POST routes."""
        self._dispatch("POST")

    def do_PUT(self) -> None:
        """Serve module routes that accept PUT."""
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        """Serve module routes that accept PATCH."""
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        """Serve module routes that accept DELETE."""
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        """Answer one request, and swallow only a socket that closed while answering it.

        The chassis stops while a client may still be mid-request. A pipe or descriptor
        that vanished is recorded and ends this connection; any other error is left to
        rise, because a handler that hides a real fault answers nothing at all.
        """
        try:
            self._route(method)
        except OSError as exc:
            if not _is_teardown_error(exc):
                raise
            self.close_connection = True
            self._note_socket_gone(exc, f"{method} dispatch")

    def _route(self, method: str) -> None:
        ipc = cast("_ControlServer", self.server).kdx_ipc
        path = normalize_route_path(self.path)
        module_route = ipc.lookup_route(path)
        if path not in ROUTES and module_route is None:
            mounted = ipc.lookup_static(path)
            if mounted is not None:
                self._serve_static(ipc, mounted[0], mounted[1], method)
                return
            self._reply(404, {"error": f"no such endpoint: {path}"})
            return
        if module_route is not None:
            self._serve_module_route(ipc, module_route, method)
            return
        if ipc.requires_token(path) and not ipc.token_matches(self.headers.get(TOKEN_HEADER)):
            self._reply(
                401,
                {
                    "error": f"{TOKEN_HEADER} header missing or invalid for {path}",
                    "hint": "set core.ipc.auth to false for anonymous reads, or "
                    "core.ipc.allow_anonymous_shutdown to true for anonymous shutdown",
                },
            )
            return
        if path == HEALTH_PATH:
            self._reply(200, ipc.health())
            return
        if path == SCHEMA_PATH:
            if method != "GET":
                self._reply(405, {"error": "schema expects GET"})
                return
            self._reply(200, ipc.api_contract())
            return
        if path == STATUS_PATH:
            if method != "GET":
                self._reply(405, {"error": "status expects GET"})
                return
            self._reply(200, ipc.status_snapshot())
            return
        if path == RELOAD_PATH:
            if method != "POST":
                self._reply(405, {"error": "reload expects POST"})
                return
            self._reply(200, ipc.request_reload())
            return
        if path == RPC_PATH:
            if method != "POST":
                self._reply(405, {"error": "rpc expects POST"})
                return
            payload = self._read_payload()
            if payload is PAYLOAD_TOO_LARGE:
                self._reply(PAYLOAD_TOO_LARGE, {"error": "request body too large"})
                return
            self._reply(*ipc.handle_rpc(payload))
            return
        if method != "POST":
            self._reply(405, {"error": "shutdown expects POST"})
            return
        # Ask the chassis to stop before committing the response: 'accepted: true'
        # is the contract that the shutdown was requested, so a client that sees it
        # must be able to rely on it having happened. Replying first raced the call
        # on slow runners, where the response landed before the counter moved.
        ipc.request_shutdown()
        self._reply(200, {"accepted": True, "state": ipc.health()["state"]})

    def _serve_module_route(self, ipc: IpcServer, route: IpcRoute, method: str) -> None:
        """Answer one module-declared route, mapping the handler result to HTTP."""
        if not route.allows(method):
            self._reply(
                405,
                {"error": f"{route.path} accepts {', '.join(route.methods)}", "method": method},
            )
            return
        if route.auth and not ipc.token_matches(self.headers.get(TOKEN_HEADER)):
            self._reply(
                401,
                {
                    "error": f"{TOKEN_HEADER} header missing or invalid for {route.path}",
                    "hint": "this module route requires the shared control-plane token",
                },
            )
            return
        request = IpcRequest(
            method=method,
            path=route.path,
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query),
            payload=self._read_payload(),
            headers=dict(self.headers.items()),
        )
        try:
            result = route.handler(request)
        except Exception as exc:
            ipc.count_error()
            self._reply(
                500,
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "route": route.path,
                    "module": route.module,
                },
            )
            return
        status: int = 200
        payload: Any = result
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            status, payload = cast("tuple[int, Any]", result)
        self._reply_value(status, payload, route)

    def _reply_value(self, status: int, payload: Any, route: IpcRoute) -> None:
        """Answer with whatever the handler returned, keeping its meaning intact.

        An IpcResponse owns its media type, bytes keep theirs, and anything else
        is rendered as JSON exactly as it always was - so a module that never
        heard of content types still gets the v1 behaviour for free.
        """
        if isinstance(payload, IpcResponse):
            # The response owns its status and media type; the route only decided
            # the path, so 'IpcResponse.text(..., status=201)' means 201.
            self._send(payload.status, payload.body, payload.content_type, payload.headers)
            return
        if isinstance(payload, bytearray | memoryview | bytes):
            self._send(status, bytes(payload), route.binary_content_type())
            return
        if isinstance(payload, str) and route.declared_content_type() != CONTENT_JSON:
            self._send(status, payload.encode("utf-8"), route.declared_content_type())
            return
        self._reply(status, payload)

    def _serve_static(
        self, ipc: IpcServer, mount: IpcStaticMount, subpath: str, method: str
    ) -> None:
        """Serve one file from a mounted static directory, with its own media type."""
        if not mount.allows(method):
            self._reply(
                405,
                {
                    "error": f"{mount.prefix} serves {', '.join(STATIC_METHODS)}",
                    "method": method,
                },
            )
            return
        if mount.auth and not ipc.token_matches(self.headers.get(TOKEN_HEADER)):
            self._reply(
                401,
                {
                    "error": f"{TOKEN_HEADER} header missing or invalid for {mount.prefix}",
                    "hint": "this static mount requires the shared control-plane token",
                },
            )
            return
        target = mount.resolve(subpath)
        if target is None:
            self._reply(
                403,
                {
                    "error": f"{subpath!r} cannot read outside the mounted directory",
                    "prefix": mount.prefix,
                },
            )
            return
        if target.is_dir():
            target = _index_file(mount, target)
        if target is None or not target.is_file():
            self._reply(404, {"error": f"no such file below {mount.prefix}", "path": f"/{subpath}"})
            return
        try:
            data = target.read_bytes()
            stamp = target.stat()
        except OSError as exc:
            ipc.count_error()
            self._reply(500, {"error": f"cannot read {target.name}: {exc}", "prefix": mount.prefix})
            return
        etag = f'W/"{int(stamp.st_mtime)}-{len(data)}"'
        headers: tuple[tuple[str, str], ...] = (
            ("Cache-Control", mount.cache_control),
            ("ETag", etag),
        )
        if str(self.headers.get("If-None-Match") or "").strip() == etag:
            self._send(304, b"", CONTENT_PLAIN, headers, body=False)
            return
        self._send(
            200,
            data,
            guess_content_type(target.name),
            headers,
            body=str(method).upper() != "HEAD",
        )

    def _read_payload(self) -> Any:
        """Return the decoded JSON request body, or None when there is none.

        A body whose ``Content-Length`` exceeds :data:`MAX_IPC_PAYLOAD_BYTES` is
        refused with :data:`PAYLOAD_TOO_LARGE` before any of it is read, so an
        oversized request cannot exhaust the daemon memory. The caller answers
        with the status this returns; a body within the limit decodes as before.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return None
        if length > MAX_IPC_PAYLOAD_BYTES:
            return PAYLOAD_TOO_LARGE
        raw = self.rfile.read(length)
        return _decode(raw)

    def _reply(self, code: int, payload: Any, *, content_type: str = CONTENT_JSON) -> None:
        """Answer with a JSON body, the control plane's default rendering."""
        raw = json.dumps(payload, default=str).encode("utf-8")
        self._send(code, raw, content_type)

    def _send(
        self,
        code: int,
        raw: bytes,
        content_type: str,
        headers: Sequence[tuple[str, str]] = (),
        *,
        body: bool = True,
    ) -> None:
        """Write one response, whatever its media type, with the given headers.

        The client may stop reading, or the chassis may stop serving, between the status
        line and the body: a socket that closed mid-response is answered quietly here too.
        """
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            if code != 304:
                self.send_header("Content-Length", str(len(raw)))
            for key, value in headers:
                self.send_header(str(key), str(value))
            self.send_header("Connection", "close")
            # The header alone does not end the dispatch loop. A client that asked for
            # HTTP/1.1 keep-alive would leave this handler waiting for the next request
            # on a socket the chassis never reuses, so the handler says the same thing the
            # header says and the connection closes after this response.
            self.close_connection = True
            self.end_headers()
            if body and code != 304:
                self.wfile.write(raw)
        except OSError as exc:
            if not _is_teardown_error(exc):
                raise
            self._note_socket_gone(exc, f"response {code}")

    def _note_socket_gone(self, exc: OSError, where: str) -> None:
        """Say once that the socket closed underneath this response.

        A control plane that is stopping has decided to stop talking: a client that is
        still mid-request is the expected ending, so it is logged at debug level. The same
        socket error while the plane is running is worth one line to the operator.
        """
        ipc = getattr(self.server, "kdx_ipc", None)
        logger = getattr(ipc, "logger", None)
        if logger is None:
            return
        running = bool(getattr(ipc, "running", True))
        level = logger.warning if running else logger.debug
        message = "ipc socket closed mid-response" if running else "ipc socket closed at shutdown"
        level(message, path=str(self.path), where=where, error=str(exc))

    def log_message(self, format: str, *args: Any) -> None:
        """Route request logging to the chassis logger, never to stderr directly."""
        ipc = getattr(self.server, "kdx_ipc", None)
        if ipc is not None and ipc.logger is not None:
            ipc.logger.debug("ipc " + (format % args), path=self.path)


class IpcServer:
    """Loopback HTTP server exposing health, status, and remote shutdown."""

    def __init__(
        self,
        *,
        status_fn: Callable[[], dict[str, Any]],
        shutdown_fn: Callable[[], None],
        state_fn: Callable[[], str],
        options: IpcOptions | None = None,
        logger: Any = None,
        routes: Sequence[IpcRoute] | None = None,
        static_dirs: Sequence[IpcStaticMount] | None = None,
        rpc_fn: Callable[[str, Mapping[str, Any]], Any] | None = None,
        reload_fn: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._status_fn = status_fn
        self._shutdown_fn = shutdown_fn
        self._state_fn = state_fn
        self._options = options or IpcOptions()
        self.logger = logger
        self._httpd: _ControlServer | None = None
        self._thread: threading.Thread | None = None
        self._discovery: IpcDiscovery | None = None
        self._requests = 0
        self._errors = 0
        self._started_at = 0.0
        self._bound_host = self._options.host
        self._lan_accessible = not is_loopback_host(self._options.host)
        self._routes: dict[str, IpcRoute] = {}
        for route in routes or ():
            self.add_route(route)
        self._static: dict[str, IpcStaticMount] = {}
        for mount in static_dirs or ():
            self.add_static_dir(mount)
        self._rpc_fn = rpc_fn
        self._reload_fn = reload_fn

    def add_route(self, route: IpcRoute) -> IpcServer:
        """Publish *route* on the control plane; a re-registration replaces it."""
        self._routes[route.path] = route
        return self

    def remove_route(self, path: str) -> bool:
        """Withdraw the route registered for *path*; return whether one existed."""
        return self._routes.pop(normalize_route_path(path), None) is not None

    def lookup_route(self, path: str) -> IpcRoute | None:
        """Return the module route registered for *path*, or None."""
        return self._routes.get(normalize_route_path(path))

    def module_routes(self) -> list[IpcRoute]:
        """Return every module-contributed route, ordered by path."""
        return [self._routes[path] for path in sorted(self._routes)]

    def add_static_dir(self, mount: IpcStaticMount) -> IpcServer:
        """Publish *mount* on the control plane; a re-registration replaces it."""
        reserved = {normalize_route_path(path) for path in ROUTES}
        if mount.prefix in reserved:
            raise IpcError(
                f"static prefix {mount.prefix!r} is reserved by the chassis control plane",
                hint="mount your UI below the core paths, for example '/ui'",
            )
        taken = self.lookup_route(mount.prefix)
        if taken is not None:
            raise IpcError(
                f"static prefix {mount.prefix!r} collides with the route {taken.path!r}",
                hint="give the mount and the route different paths",
            )
        self._static[mount.prefix] = mount
        return self

    def register_static_dir(
        self,
        route_prefix: str,
        directory: str | Path,
        *,
        index: str | Sequence[str] = "index.html",
        cache_control: str = "no-cache",
        auth: bool = False,
        module: str = "",
    ) -> IpcStaticMount:
        """Serve *directory* under *route_prefix*; return the mount created.

        The chassis owns the socket, so a module gets a dashboard or an asset tree
        onto the same port as its JSON without opening a second server. Prefix
        collisions replace, so a module that re-registers on reload stays honest.
        """
        names: tuple[str, ...] = (
            (str(index),) if isinstance(index, str) else tuple(str(item) for item in index)
        )
        mount = IpcStaticMount(
            route_prefix,
            Path(directory),
            index_names=names or ("index.html",),
            cache_control=cache_control,
            auth=auth,
            module=module,
        )
        self.add_static_dir(mount)
        return mount

    def unregister_static_dir(self, route_prefix: str) -> bool:
        """Withdraw the mount registered for *route_prefix*; return whether one existed."""
        clean = normalize_route_path(route_prefix).rstrip("/")
        return self._static.pop(clean, None) is not None

    def static_mounts(self) -> list[IpcStaticMount]:
        """Return every mounted directory, longest prefix first so matching is stable."""
        return sorted(self._static.values(), key=lambda item: (-len(item.prefix), item.prefix))

    def lookup_static(self, path: str) -> tuple[IpcStaticMount, str] | None:
        """Return the mount answering *path* and the file path below it, or None."""
        for mount in self.static_mounts():
            if mount.matches(path):
                return mount, mount.relative(path)
        return None

    def token_matches(self, presented: str | None) -> bool:
        """Return True when *presented* is the shared secret, in constant time."""
        expected = self.discovery_token
        return hmac.compare_digest(str(presented or ""), str(expected))

    def count_error(self) -> None:
        """Record that a module route handler raised."""
        self._errors += 1

    def request_reload(self) -> dict[str, Any]:
        """Ask the chassis to re-read its configuration; 501 when unsupported."""
        self._requests += 1
        if self._reload_fn is None:
            return {"reloaded": False, "reason": "this control plane has no reload hook"}
        return {"reloaded": True, **dict(self._reload_fn())}

    def handle_rpc(self, payload: Any) -> tuple[int, dict[str, Any]]:
        """Delegate one command to the running chassis; return (status, body)."""
        self._requests += 1
        if self._rpc_fn is None:
            return 501, {"ok": False, "error": "this control plane accepts no rpc calls"}
        if not isinstance(payload, Mapping):
            return 400, {"ok": False, "error": "rpc expects a JSON object body"}
        command = str(payload.get("command") or "").strip()
        if not command:
            return 400, {"ok": False, "error": "rpc body needs a 'command' field"}
        raw_args = payload.get("args")
        args: dict[str, Any] = dict(raw_args) if isinstance(raw_args, Mapping) else {}
        try:
            result = self._rpc_fn(command, args)
        except LookupError as exc:
            return 404, {"ok": False, "command": command, "error": str(exc)}
        except TypeError as exc:
            return 400, {"ok": False, "command": command, "error": str(exc)}
        except Exception as exc:
            self._errors += 1
            return 500, {
                "ok": False,
                "command": command,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return 200, {"ok": True, "command": command, "result": result}

    def start(self) -> IpcServer:
        """Bind to the loopback interface, announce the endpoint, serve in background."""
        if self._httpd is not None:
            return self
        host = self._options.host
        httpd: _ControlServer | None = None
        last_error: OSError | None = None
        for attempt in range(max(1, self._options.fallback_attempts)):
            port = self._options.port if self._options.port else 0
            candidate = port + attempt if port else 0
            try:
                httpd = _ControlServer((host, candidate), _ControlHandler)
                break
            except OSError as exc:
                last_error = exc
                if not port:
                    break
        if httpd is None:
            raise IpcError(
                f"cannot bind the control plane to {host}: {last_error}",
                hint="Free core.ipc.port or set it to 0 to take an ephemeral port.",
            ) from last_error
        httpd.daemon_threads = True
        self._httpd = httpd
        self._started_at = time.time()
        bound_host = str(httpd.server_address[0])
        bound_port = int(httpd.server_address[1])
        # A wildcard bind is announced as loopback: that is the address a local
        # client can actually dial, and the discovery file is read locally.
        announce_host = DEFAULT_HOST if is_wildcard_host(bound_host) else bound_host
        self._lan_accessible = not is_loopback_host(bound_host)
        self._discovery = IpcDiscovery(
            pid=os.getpid(),
            host=announce_host,
            port=bound_port,
            token=self._options.token,
            started_at=self._started_at,
            path=self._options.discovery_path,
        )
        self._bound_host = bound_host
        httpd.kdx_ipc = self
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name="kdx-ipc",
            daemon=True,
        )
        self._thread.start()
        self._write_discovery()
        if self.logger is not None:
            self.logger.info(
                "ipc control plane ready",
                host=bound_host,
                port=bound_port,
                pid=os.getpid(),
                auth=self._options.auth,
                token_source=self._options.token_source,
                lan_accessible=self._lan_accessible,
                discovery=str(self._options.discovery_path)
                if self._options.discovery_path
                else None,
            )
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop accepting requests, close the socket, remove the discovery file."""
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            with contextlib.suppress(Exception):
                httpd.shutdown()
            with contextlib.suppress(Exception):
                httpd.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        self._remove_discovery()

    @property
    def discovery(self) -> IpcDiscovery | None:
        """Return the endpoint announcement (None once stopped)."""
        return self._discovery

    @property
    def discovery_token(self) -> str:
        """Return the shared secret clients must present."""
        return self._discovery.token if self._discovery is not None else self._options.token

    def requires_token(self, route: str) -> bool:
        """Return True when *route* must present the shared token.

        Policy: /health and /api/v1/schema are always open (liveness and static
        metadata carry no state). Reads follow core.ipc.auth. Shutdown stays
        authenticated unless the operator explicitly allows anonymous shutdown - a
        LAN-visible port must not be a stop button.
        """
        if route in (HEALTH_PATH, SCHEMA_PATH):
            return False
        if route in (STATUS_PATH, RPC_PATH, RELOAD_PATH):
            return bool(self._options.auth)
        return bool(self._options.auth) or not bool(self._options.allow_anonymous_shutdown)

    @property
    def bound_host(self) -> str:
        """Return the interface the server actually bound."""
        return self._bound_host

    @property
    def lan_accessible(self) -> bool:
        """Return True when something off this machine can reach the endpoint."""
        return self._lan_accessible

    @property
    def auth_enabled(self) -> bool:
        """Return True when reads require the shared token."""
        return bool(self._options.auth)

    @property
    def token_source(self) -> str:
        """Return where the shared secret came from: 'static' or 'ephemeral'."""
        return str(self._options.token_source)

    @property
    def running(self) -> bool:
        """Return True while the server is serving requests."""
        return self._httpd is not None

    def health(self) -> dict[str, Any]:
        """Return the unauthenticated liveness payload."""
        return {
            "status": "ok",
            "state": self._state_fn(),
            "uptime": round(time.time() - self._started_at, 3) if self._started_at else 0.0,
        }

    def status_snapshot(self) -> dict[str, Any]:
        """Return the full chassis report plus the IPC announcement."""
        snapshot = dict(self._status_fn())
        snapshot["ipc"] = self.as_dict()
        return snapshot

    def api_contract(self) -> dict[str, Any]:
        """Return the machine-readable contract this control plane answers.

        Route paths, auth flags, and the health and status field lists are read
        from the live handlers, so the contract cannot drift from the server.
        """
        return {
            "schema": IPC_SCHEMA_VERSION,
            "core_api": CORE_API_VERSION,
            "token_header": TOKEN_HEADER,
            "auth": bool(self._options.auth),
            "allow_anonymous_shutdown": bool(self._options.allow_anonymous_shutdown),
            "static_mounts": [mount.as_dict() for mount in self.static_mounts()],
            "routes": [
                {
                    "method": "GET",
                    "path": HEALTH_PATH,
                    "auth": False,
                    "content_type": CONTENT_JSON,
                    "summary": "liveness probe; never needs a token",
                    "fields": sorted(self.health()),
                },
                {
                    "method": "GET",
                    "path": SCHEMA_PATH,
                    "auth": False,
                    "content_type": CONTENT_JSON,
                    "summary": "this contract; static metadata",
                    "fields": [
                        "auth",
                        "core_api",
                        "module_routes",
                        "routes",
                        "schema",
                        "static_mounts",
                        "token_header",
                    ],
                },
                {
                    "method": "GET",
                    "path": STATUS_PATH,
                    "auth": bool(self._options.auth),
                    "content_type": CONTENT_JSON,
                    "summary": "full chassis report over the control plane",
                    "fields": sorted(self.status_snapshot()),
                },
                {
                    "method": "POST",
                    "path": SHUTDOWN_PATH,
                    "auth": self.requires_token(SHUTDOWN_PATH),
                    "content_type": CONTENT_JSON,
                    "summary": "operator stop; the process exits 0",
                    "fields": ["accepted", "state"],
                },
                {
                    "method": "POST",
                    "path": RELOAD_PATH,
                    "auth": self.requires_token(RELOAD_PATH),
                    "content_type": CONTENT_JSON,
                    "summary": "re-read configuration; the daemon keeps running",
                    "fields": ["reloaded", "changed"],
                },
                {
                    "method": "POST",
                    "path": RPC_PATH,
                    "auth": self.requires_token(RPC_PATH),
                    "content_type": CONTENT_JSON,
                    "summary": "delegate one command to the running chassis",
                    "fields": ["command", "ok", "result"],
                },
            ],
            "module_routes": [route.as_dict() for route in self.module_routes()],
        }

    def request_shutdown(self) -> None:
        """Ask the chassis to shut down (the caller owns the lifecycle)."""
        self._requests += 1
        self._shutdown_fn()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for status output."""
        if self._discovery is None:
            return {"running": False, "requests": self._requests}
        payload = self._discovery.as_dict()
        payload["running"] = self.running
        payload["requests"] = self._requests
        payload["path"] = str(self._discovery.path) if self._discovery.path else None
        payload["bound_host"] = self._bound_host
        payload["lan_accessible"] = self._lan_accessible
        payload["auth"] = bool(self._options.auth)
        payload["allow_anonymous_shutdown"] = bool(self._options.allow_anonymous_shutdown)
        payload["token_source"] = str(self._options.token_source)
        payload["errors"] = self._errors
        payload["routes"] = [route.as_dict() for route in self.module_routes()]
        payload["static"] = [mount.prefix for mount in self.static_mounts()]
        payload["static_mounts"] = [mount.as_dict() for mount in self.static_mounts()]
        payload.pop("token", None)
        return payload

    def _write_discovery(self) -> None:
        """Publish the discovery file so a reader never finds it half written.

        'kdx status' and an external health check read this file at any moment, including
        the first millisecond of boot. Truncating it in place showed those readers an empty
        file, which they read as a daemon that is not running, so the payload lands in a
        sibling this process owns and is renamed into place in one filesystem step.
        """
        target = self._options.discovery_path
        if target is None or self._discovery is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._discovery.as_dict(), sort_keys=True) + "\n"
        temporary = target.parent / f"{target.name}.tmp.{os.getpid()}"
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
        try:
            os.replace(str(temporary), str(target))
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        with contextlib.suppress(OSError):
            os.chmod(str(target), 0o600)

    def _remove_discovery(self) -> None:
        target = self._options.discovery_path
        if target is None:
            return
        with contextlib.suppress(OSError):
            target.unlink()


__all__ = [
    "CONTENT_HTML",
    "CONTENT_JSON",
    "CONTENT_OCTET_STREAM",
    "CONTENT_PLAIN",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DISCOVERY_NAME",
    "HEALTH_PATH",
    "LOOPBACK_HOSTS",
    "RELOAD_PATH",
    "ROUTES",
    "RPC_PATH",
    "SCHEMA_PATH",
    "SHUTDOWN_PATH",
    "STATIC_METHODS",
    "STATUS_PATH",
    "TOKEN_HEADER",
    "WILDCARD_HOSTS",
    "IpcDiscovery",
    "IpcError",
    "IpcOptions",
    "IpcRequest",
    "IpcResponse",
    "IpcRoute",
    "IpcServer",
    "IpcStaticMount",
    "guess_content_type",
    "is_loopback_host",
    "is_wildcard_host",
    "new_token",
    "normalize_route_path",
    "process_memory",
]
