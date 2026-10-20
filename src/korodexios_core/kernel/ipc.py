"""Local HTTP control plane: health probe, status snapshot, remote shutdown.

The chassis is headless-first, so a running daemon must be observable from the
outside without a heavyweight dependency. A stdlib ThreadingHTTPServer bound to
the loopback interface gives the CLI, curl, Docker HEALTHCHECK, and future web
dashboards the same JSON on Linux, macOS, and Windows.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

DISCOVERY_NAME = "chassis.ipc"
TOKEN_HEADER = "X-Korodex-Token"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8123
HEALTH_PATH = "/health"
STATUS_PATH = "/api/v1/status"
SHUTDOWN_PATH = "/api/v1/shutdown"
ROUTES = (HEALTH_PATH, STATUS_PATH, SHUTDOWN_PATH)


class IpcError(ValueError):
    """Raised when the control plane cannot be started or reached."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def process_memory() -> dict[str, Any]:
    """Return this process's resident memory, best effort, cross-platform."""
    result: dict[str, Any] = {"rss_bytes": None, "rss": "-", "source": "unavailable"}
    statm = Path("/proc/self/statm")
    try:
        if statm.is_file():
            resident_pages = int(statm.read_text(encoding="utf-8").split()[1])
            page_size = os.sysconf("SC_PAGE_SIZE")
            rss = max(0, resident_pages) * int(page_size)
            result.update(rss_bytes=rss, rss=_human_bytes(rss), source="proc/statm")
            return result
    except (OSError, ValueError, IndexError):
        return result
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        scale = 1 if os.name == "posix" and sys_is_linux() else 1024
        rss = int(usage) * scale
        result.update(rss_bytes=rss, rss=_human_bytes(rss), source="getrusage")
    except (ImportError, ValueError, OSError):
        pass
    return result


def sys_is_linux() -> bool:
    """Return True when ru_maxrss is already reported in bytes (Linux)."""
    import platform

    return platform.system().lower() == "linux"


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
        """Return the loopback base URL for this endpoint."""
        return f"http://{self.host}:{self.port}"

    def is_process_alive(self) -> bool:
        """Return True when the recorded pid still exists."""
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    def request(
        self,
        route: str = STATUS_PATH,
        *,
        method: str = "GET",
        timeout: float = 2.0,
    ) -> tuple[int, Any]:
        """Call one route with the shared token; return (status, decoded body)."""
        request = urllib.request.Request(
            self.base_url() + route,
            method=method,
            headers={TOKEN_HEADER: self.token, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), _decode(response.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), _decode(exc.read())

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
    token: str = field(default_factory=lambda: secrets.token_hex(16))
    fallback_attempts: int = 5


class _ControlServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that knows which chassis control plane it serves."""

    korodex_ipc: IpcServer


class _ControlHandler(BaseHTTPRequestHandler):
    """Routes the three control-plane endpoints and enforces the token."""

    protocol_version = "HTTP/1.1"
    server_version = "korodexios"

    def do_GET(self) -> None:
        """Serve /health and /api/v1/status."""
        self._dispatch("GET")

    def do_POST(self) -> None:
        """Serve /api/v1/shutdown."""
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        ipc = cast("_ControlServer", self.server).korodex_ipc
        route = self.path.split("?", 1)[0]
        if route not in ROUTES:
            self._reply(404, {"error": f"no such endpoint: {route}"})
            return
        if route != HEALTH_PATH and self.headers.get(TOKEN_HEADER) != ipc.discovery_token:
            self._reply(
                401,
                {"error": "missing or invalid X-Korodex-Token header"},
            )
            return
        if route == HEALTH_PATH:
            self._reply(200, ipc.health())
            return
        if route == STATUS_PATH:
            if method != "GET":
                self._reply(405, {"error": "status expects GET"})
                return
            self._reply(200, ipc.status_snapshot())
            return
        if method != "POST":
            self._reply(405, {"error": "shutdown expects POST"})
            return
        self._reply(200, {"accepted": True, "state": ipc.health()["state"]})
        ipc.request_shutdown()

    def _reply(self, code: int, payload: Any) -> None:
        raw = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        """Route request logging to the chassis logger, never to stderr directly."""
        ipc = getattr(self.server, "korodex_ipc", None)
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
        self._started_at = 0.0

    def start(self) -> IpcServer:
        """Bind to the loopback interface, announce the endpoint, serve in background."""
        if self._httpd is not None:
            return self
        host = self._options.host
        if host not in (DEFAULT_HOST, "localhost", "127.0.0.1"):
            raise IpcError(
                f"the control plane must stay on the loopback interface, got {host!r}",
                hint="Set core.ipc.host to 127.0.0.1 to keep the token meaningful.",
            )
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
        self._discovery = IpcDiscovery(
            pid=os.getpid(),
            host=bound_host,
            port=bound_port,
            token=self._options.token,
            started_at=self._started_at,
            path=self._options.discovery_path,
        )
        httpd.korodex_ipc = self
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name="korodex-ipc",
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
        payload.pop("token", None)
        return payload

    def _write_discovery(self) -> None:
        target = self._options.discovery_path
        if target is None or self._discovery is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._discovery.as_dict(), sort_keys=True)
        descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        with contextlib.suppress(OSError):
            os.chmod(str(target), 0o600)

    def _remove_discovery(self) -> None:
        target = self._options.discovery_path
        if target is None:
            return
        with contextlib.suppress(OSError):
            target.unlink()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DISCOVERY_NAME",
    "HEALTH_PATH",
    "SHUTDOWN_PATH",
    "STATUS_PATH",
    "TOKEN_HEADER",
    "IpcDiscovery",
    "IpcError",
    "IpcOptions",
    "IpcServer",
    "process_memory",
]
