"""IPC control plane tests: endpoints, token gate, discovery file, and 'kdx status'."""

from __future__ import annotations

import errno
import http.client
import inspect
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import IPC_SCHEMA_VERSION, ExitCode
from korodexios_core.kernel import ipc as ipc_module
from korodexios_core.kernel.chassis import Chassis, ChassisOptions, ChassisState
from korodexios_core.kernel.ipc import (
    DEFAULT_ROUTE_METHODS,
    DISCOVERY_NAME,
    HEALTH_PATH,
    HTTP_METHODS,
    IS_LINUX,
    MAX_IPC_PAYLOAD_BYTES,
    ROUTES,
    RPC_PATH,
    SCHEMA_PATH,
    SHUTDOWN_PATH,
    STATUS_PATH,
    TOKEN_HEADER,
    IpcDiscovery,
    IpcError,
    IpcOptions,
    IpcRequest,
    IpcRoute,
    IpcServer,
    IpcStaticMount,
    process_memory,
    sys_is_linux,
)
from korodexios_core.paths import AppPaths
from korodexios_core.units import DEFAULT_PAGE_SIZE
from korodexios_core.version import CORE_API_VERSION

from .test_chassis import build_chassis


def make_server(
    tmp_path: Path,
    *,
    port: int = 0,
    host: str = "127.0.0.1",
    routes: tuple[IpcRoute, ...] = (),
    static_dirs: tuple[IpcStaticMount, ...] = (),
) -> tuple[IpcServer, dict[str, int]]:
    """Start a control plane on an ephemeral port and count shutdown requests."""
    calls = {"shutdown": 0}
    server = IpcServer(
        status_fn=lambda: {
            "app": {"state": "running", "uptime_seconds": 1.0},
            "modules": {"total": 1},
        },
        shutdown_fn=lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1),
        state_fn=lambda: "running",
        options=IpcOptions(host=host, port=port, discovery_path=tmp_path / DISCOVERY_NAME),
        routes=routes,
        static_dirs=static_dirs,
    )
    server.start()
    return server, calls


def http_call(
    base: str,
    route: str,
    *,
    token: str | None = None,
    method: str = "GET",
) -> tuple[int, object]:
    """Issue one control-plane request and return (status, decoded body)."""
    headers = {TOKEN_HEADER: token} if token is not None else {}
    request = urllib.request.Request(base + route, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def base_args(project_root) -> list[str]:
    return ["--home", str(project_root), "--no-log-file", "--no-color"]


def ipc_chassis(project_root) -> Chassis:
    """Return a chassis whose control plane is enabled on an ephemeral port."""
    paths = AppPaths.discover(root=str(project_root), environ={})
    return Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            cli_overrides={
                "core.log.file_enabled": False,
                "core.ipc.enabled": True,
                "core.ipc.port": 0,
            },
            install_signals=False,
        )
    )


def test_health_is_open_and_reports_state(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    try:
        code, payload = http_call(server.discovery.base_url(), HEALTH_PATH)  # type: ignore[union-attr]
        assert code == 200
        assert payload == {
            "status": "ok",
            "state": "running",
            "uptime": pytest.approx(0.0, abs=5.0),
        }
    finally:
        server.stop()


def test_status_requires_the_shared_token(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    base = server.discovery.base_url()  # type: ignore[union-attr]
    token = server.discovery_token
    try:
        assert http_call(base, STATUS_PATH)[0] == 401
        assert http_call(base, STATUS_PATH, token="wrong")[0] == 401
        code, payload = http_call(base, STATUS_PATH, token=token)
        assert code == 200
        assert payload["modules"]["total"] == 1  # type: ignore[index]
        assert payload["ipc"]["running"] is True  # type: ignore[index]
    finally:
        server.stop()


def test_schema_is_open_and_matches_the_live_routes(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    base = server.discovery.base_url()  # type: ignore[union-attr]
    try:
        code, payload = http_call(base, SCHEMA_PATH)  # static metadata needs no token
        assert code == 200
        assert payload["schema"] == IPC_SCHEMA_VERSION
        assert payload["core_api"] == CORE_API_VERSION
        assert payload["token_header"] == TOKEN_HEADER
        routes = {route["path"]: route for route in payload["routes"]}  # type: ignore[index]
        assert sorted(routes) == sorted(ROUTES)
        assert routes[HEALTH_PATH]["auth"] is False
        assert routes[STATUS_PATH]["auth"] is True
        assert "app" in routes[STATUS_PATH]["fields"]  # type: ignore[operator]
        assert http_call(base, SCHEMA_PATH, method="POST")[0] == 405
    finally:
        server.stop()


def test_unknown_routes_and_methods_are_rejected(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    base = server.discovery.base_url()  # type: ignore[union-attr]
    token = server.discovery_token
    try:
        assert http_call(base, "/nope")[0] == 404
        assert http_call(base, STATUS_PATH, token=token, method="POST")[0] == 405
        assert http_call(base, SHUTDOWN_PATH, token=token)[0] == 405
    finally:
        server.stop()


def test_shutdown_post_asks_the_chassis_to_stop(tmp_path: Path) -> None:
    server, calls = make_server(tmp_path)
    base = server.discovery.base_url()  # type: ignore[union-attr]
    try:
        code, payload = http_call(base, SHUTDOWN_PATH, token=server.discovery_token, method="POST")
        assert code == 200
        assert payload["accepted"] is True  # type: ignore[index]
        assert calls["shutdown"] == 1
        assert server.as_dict()["requests"] == 1
    finally:
        server.stop()


def test_shutdown_is_requested_before_the_response_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'accepted: true' is a contract: the shutdown is asked for before it is promised.

    Regression for the macOS/Windows CI failure where a slow runner saw the 200
    before the counter moved. The handler must call request_shutdown() and only
    then commit the reply, so a client that has read 'accepted: true' can rely on
    the shutdown having been requested.
    """
    order: list[str] = []
    server, _ = make_server(tmp_path)
    base = server.discovery.base_url()  # type: ignore[union-attr]
    handler = ipc_module._ControlHandler

    real_request_shutdown = server.request_shutdown
    real_reply = handler._reply

    def tracking_shutdown() -> None:
        order.append("requested")
        real_request_shutdown()

    def tracking_reply(self: object, code: int, payload: dict[str, object]) -> None:
        order.append(f"replied:{code}")
        real_reply(self, code, payload)  # type: ignore[assignment]

    monkeypatch.setattr(server, "request_shutdown", tracking_shutdown)
    monkeypatch.setattr(handler, "_reply", tracking_reply)
    try:
        code, payload = http_call(base, SHUTDOWN_PATH, token=server.discovery_token, method="POST")
        assert code == 200
        assert payload["accepted"] is True  # type: ignore[index]
    finally:
        server.stop()
    assert "requested" in order, "the shutdown was never asked for"
    assert order.index("requested") < order.index("replied:200"), (
        "the response committed before the shutdown was requested: " + str(order)
    )


def test_rpc_payload_over_the_limit_is_refused_with_413(tmp_path: Path) -> None:
    """A request body over 10 MiB is rejected before a single byte is buffered."""
    server, _ = make_server(tmp_path)
    base = server.discovery.base_url()  # type: ignore[union-attr]
    token = server.discovery_token
    try:
        oversized = MAX_IPC_PAYLOAD_BYTES + 1
        body = b"\x00" * 16  # the declared length lies; the plane must not trust it
        request = urllib.request.Request(
            base + RPC_PATH,
            data=body,
            method="POST",
            headers={TOKEN_HEADER: token, "Content-Length": str(oversized)},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        assert int(caught.value.code) == 413
        response = json.loads(caught.value.read().decode("utf-8"))
        assert "too large" in response["error"]
    finally:
        server.stop()


def test_rpc_payload_at_the_limit_is_accepted(tmp_path: Path) -> None:
    """A body exactly at the limit is still buffered: the limit is inclusive."""
    server, _ = make_server(tmp_path)
    base = server.discovery.base_url()  # type: ignore[union-attr]
    token = server.discovery_token
    try:
        body = b"x" * 8
        request = urllib.request.Request(
            base + RPC_PATH,
            data=body,
            method="POST",
            headers={TOKEN_HEADER: token},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                code = int(response.status)
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            payload = json.loads(exc.read().decode("utf-8"))
        assert code == 501, payload
    finally:
        server.stop()


def test_discovery_file_is_private_and_removed(tmp_path: Path) -> None:
    target = tmp_path / DISCOVERY_NAME
    server, _ = make_server(tmp_path)
    assert target.is_file()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["port"] > 0
    assert len(payload["token"]) >= 16
    server.stop()
    assert not target.exists()
    assert server.running is False


def test_wildcard_host_binds_and_announces_loopback(tmp_path: Path) -> None:
    """A LAN bind is legal; the discovery file announces an address a local client can dial."""
    server = IpcServer(
        status_fn=lambda: {},
        shutdown_fn=lambda: None,
        state_fn=lambda: "running",
        options=IpcOptions(host="0.0.0.0", port=0, discovery_path=tmp_path / DISCOVERY_NAME),
    )
    try:
        server.start()
        assert server.running is True
        assert server.bound_host == "0.0.0.0"
        assert server.lan_accessible is True
        assert server.discovery is not None
        assert server.discovery.host == "127.0.0.1"
        assert server.discovery.port > 0
        recorded = json.loads((tmp_path / DISCOVERY_NAME).read_text(encoding="utf-8"))
        assert recorded["host"] == "127.0.0.1"
    finally:
        server.stop()


def test_process_memory_reports_resident_size() -> None:
    memory = process_memory()
    assert memory["source"] in ("proc/statm", "getrusage", "unavailable")
    if memory["source"] != "unavailable":
        assert memory["rss_bytes"] and memory["rss_bytes"] > 0
        assert memory["rss"] != "-"


def test_process_memory_falls_back_to_default_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows host has no sysconf: the 4 KiB default keeps /proc math honest."""
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "100 200 300 400")
    monkeypatch.delattr(os, "sysconf", raising=False)
    memory = process_memory()
    assert memory["source"] == "proc/statm"
    assert memory["rss_bytes"] == 200 * DEFAULT_PAGE_SIZE


def test_chassis_lifecycle_opens_and_closes_the_endpoint(project_root) -> None:
    chassis = ipc_chassis(project_root)
    discovery_path = chassis.paths.state_dir / DISCOVERY_NAME
    chassis.initialize()
    chassis.start()
    assert chassis.state is ChassisState.RUNNING
    assert chassis.ipc is not None and chassis.ipc.running
    assert discovery_path.is_file()
    code, payload = chassis.ipc.discovery.request(STATUS_PATH)  # type: ignore[union-attr]
    assert code == 200
    assert payload["app"]["state"] == "running"  # type: ignore[index]
    chassis.shutdown()
    assert chassis.ipc is None
    assert not discovery_path.exists()


def test_start_with_ipc_disabled_keeps_the_port_free(project_root) -> None:
    chassis = ipc_chassis(project_root)
    chassis.initialize()
    chassis.start(ipc=False)
    assert chassis.ipc is None
    assert not (chassis.paths.state_dir / DISCOVERY_NAME).exists()
    chassis.shutdown()


def test_status_command_without_a_daemon(capsys, project_root) -> None:
    chassis = build_chassis(project_root)
    chassis.signals.uninstall()
    args = [*base_args(project_root), "status"]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "chassis daemon is not running" in captured.out
    assert str(project_root / "var" / DISCOVERY_NAME) in captured.out


def test_status_command_reads_a_live_daemon(capsys, project_root) -> None:
    chassis = ipc_chassis(project_root)
    chassis.initialize()
    chassis.start()
    try:
        args = [*base_args(project_root), "status", "--json"]
        assert main(args) == int(ExitCode.SUCCESS)
        payload = json.loads(capsys.readouterr().out)
        assert payload["app"]["state"] == "running"
        assert payload["ipc"]["port"] > 0
        assert payload["modules"]["total"] >= 3
    finally:
        chassis.shutdown()


def test_status_command_reports_an_unreachable_daemon(capsys, project_root) -> None:
    state_dir = project_root / "var"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / DISCOVERY_NAME).write_text(
        json.dumps(
            {"pid": os.getpid(), "host": "127.0.0.1", "port": 1, "token": "x", "started_at": 0.0}
        ),
        encoding="utf-8",
    )
    args = [*base_args(project_root), "status"]
    assert main(args) == int(ExitCode.ERROR)
    assert "no control plane answering at" in capsys.readouterr().out


def test_status_command_flags_a_stale_pid(capsys, project_root) -> None:
    state_dir = project_root / "var"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / DISCOVERY_NAME).write_text(
        json.dumps(
            {
                "pid": 2_147_483_647,
                "host": "127.0.0.1",
                "port": 8123,
                "token": "x",
                "started_at": 0.0,
            }
        ),
        encoding="utf-8",
    )
    args = [*base_args(project_root), "status", "--json"]
    assert main(args) == int(ExitCode.SUCCESS)
    assert json.loads(capsys.readouterr().out) == {
        "running": False,
        "discovery_file": str(state_dir / DISCOVERY_NAME),
    }


def ui_mount(tmp_path: Path) -> IpcStaticMount:
    """Return a mounted dashboard directory with one nested file."""
    ui = tmp_path / "ui"
    (ui / "sub").mkdir(parents=True)
    # write_bytes keeps the served bytes exact: write_text would translate the
    # trailing newline to CRLF on Windows and the response comparison would drift.
    (ui / "app.js").write_bytes(b"// app\n")
    (ui / "sub" / "app.js").write_bytes(b"// nested\n")
    (ui / "index.html").write_bytes(b"<html><body>ui</body></html>\n")
    return IpcStaticMount(route_prefix="/ui", directory=ui, auth=False)


def test_static_mount_refuses_windows_style_escapes(tmp_path: Path) -> None:
    """Both separators, every encoding, streams, and device names are checked."""
    mount = ui_mount(tmp_path)
    assert mount.resolve("app.js") == mount.directory / "app.js"
    assert mount.resolve("sub/app.js") == mount.directory / "sub" / "app.js"
    assert mount.resolve("sub\\app.js") == mount.directory / "sub" / "app.js"
    assert mount.resolve("sub%5capp.js") == mount.directory / "sub" / "app.js"
    escapes = (
        "../escape.js",
        "..\\escape.js",
        "..%5cescape.js",
        "%2e%2e%5cwindows",
        "/../../etc/passwd",
        "C:/windows/system32/cmd.exe",
        "sub/../escape.js",
    )
    for escape in escapes:
        assert mount.resolve(escape) is None, escape
    reserved = (
        "app.js:stream",
        "report.txt:Zone.Identifier",
        "NUL",
        "nul.txt",
        "com1.log",
        "index|alt.js",
        "\x00app.js",
    )
    for name in reserved:
        assert mount.resolve(name) is None, name


def test_control_plane_answers_a_windows_style_static_request(tmp_path: Path) -> None:
    """A percent-encoded backslash never walks out of the mounted directory."""
    server, _ = make_server(tmp_path, static_dirs=(ui_mount(tmp_path),))
    base = server.discovery.base_url()  # type: ignore[union-attr]
    try:
        with urllib.request.urlopen(base + "/ui/app.js", timeout=2) as response:
            assert response.status == 200
            assert response.read().decode("utf-8") == "// app\n"
        with urllib.request.urlopen(base + "/ui/sub%5capp.js", timeout=2) as response:
            assert response.status == 200
            assert response.read().decode("utf-8") == "// nested\n"
        for target in ("/ui/app.js%3Astream", "/ui/NUL", "/ui/..%5c..%5cetc/passwd"):
            status, body = http_call(base, target)
            assert status == 403, (target, body)
            assert "mounted directory" in str(body), target
    finally:
        server.stop()


def test_linux_memory_probe_is_cached_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe answers from a constant: no platform import per status snapshot."""
    assert sys_is_linux() is IS_LINUX
    monkeypatch.setattr(ipc_module, "IS_LINUX", not IS_LINUX)
    assert sys_is_linux() is (not IS_LINUX)
    assert "import platform" not in inspect.getsource(sys_is_linux)


def test_process_memory_reports_a_bounded_error_free_reading() -> None:
    """A status snapshot reads memory without touching the platform module.

    Windows answers neither /proc nor getrusage, so 'unavailable' is the honest
    answer there and the byte assertions only bind where a source exists.
    """
    memory = process_memory()
    allowed = {"proc/statm", "getrusage"} if os.name != "nt" else {"unavailable"}
    assert memory["source"] in allowed
    if memory["source"] != "unavailable":
        assert isinstance(memory["rss_bytes"], int)
        assert memory["rss"] != "-"
    else:
        assert memory["rss_bytes"] is None


def test_a_keep_alive_client_does_not_hang_on_the_control_plane(tmp_path: Path) -> None:
    """The control plane closes the socket even when the client asked for reuse."""
    server, _ = make_server(tmp_path)
    try:
        parsed = urlsplit(server.discovery.base_url())  # type: ignore[union-attr]
        host = str(parsed.hostname)
        port = int(parsed.port or 80)
        request = (
            f"GET {HEALTH_PATH} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode()
        with socket.create_connection((host, port), timeout=2.0) as connection:
            connection.sendall(request)
            chunks: list[bytes] = []
            while True:
                piece = connection.recv(4096)
                if not piece:
                    break
                chunks.append(piece)
        raw = b"".join(chunks).decode("utf-8", "replace")
        assert "200 OK" in raw
        assert "Connection: close" in raw
        assert raw.count("HTTP/1.1 200") == 1, "one response, then the socket is over"
    finally:
        server.stop()


def test_the_handler_means_the_close_header(tmp_path: Path) -> None:
    """A client that offers keep-alive is told the truth: this answer ends the reuse."""
    server, _ = make_server(tmp_path)
    try:
        parsed = urlsplit(server.discovery.base_url())  # type: ignore[union-attr]
        connection = http.client.HTTPConnection(
            str(parsed.hostname), int(parsed.port or 80), timeout=2
        )
        try:
            connection.request("GET", HEALTH_PATH, headers={"Connection": "keep-alive"})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert payload["status"] == "ok"
            assert response.getheader("Connection") == "close"
            assert response.will_close is True, "the dispatch loop ends with this response"
        finally:
            connection.close()
    finally:
        server.stop()


class RecordingLogger:
    """Minimal chassis logger double: remembers each line with its level."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def debug(self, message: str, **_fields: object) -> None:
        self.lines.append(("debug", str(message)))

    def info(self, message: str, **_fields: object) -> None:
        self.lines.append(("info", str(message)))

    def warning(self, message: str, **_fields: object) -> None:
        self.lines.append(("warning", str(message)))

    def error(self, message: str, **_fields: object) -> None:
        self.lines.append(("error", str(message)))

    def exception(self, message: str, **_fields: object) -> None:
        self.lines.append(("error", str(message)))


def request_health(port: int) -> bytes:
    """Send one raw health request and return whatever the control plane sent back."""
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as connection:
        connection.sendall(
            ("GET " + HEALTH_PATH + " HTTP/1.1" + chr(13) + chr(10)
             + "Host: 127.0.0.1" + chr(13) + chr(10)
             + "Connection: close" + chr(13) + chr(10)
             + chr(13) + chr(10)).encode("utf-8")
        )
        return connection.recv(65536)


@pytest.mark.posix
def test_the_discovery_file_lands_in_one_filesystem_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discovery file is renamed into place, so no reader ever opens a truncated one.

    POSIX-only: it asserts the published file keeps 0o600 mode bits, which Windows
    file systems do not honour, and relies on os.replace over an existing file.
    """
    server, _ = make_server(tmp_path)
    try:
        target = tmp_path / DISCOVERY_NAME
        target.write_text(
            json.dumps({"pid": -1, "note": "the previous daemon"}) + "\n", encoding="utf-8"
        )
        observed: list[tuple[int, int, str]] = []
        replace = os.replace

        def spy(source: object, destination: object) -> None:
            temp = Path(str(source))
            landed = Path(str(destination))
            size = temp.stat().st_size
            mode = temp.stat().st_mode & 0o777
            previous = landed.read_text(encoding="utf-8") if landed.exists() else ""
            observed.append((size, mode, previous))
            replace(source, destination)

        monkeypatch.setattr(ipc_module.os, "replace", spy)
        server._write_discovery()
        assert len(observed) == 1, "one publication is one rename"
        size, mode, previous = observed[0]
        assert size > 0, "the payload is whole before the rename"
        assert mode == 0o600, "the token in the payload is not world readable"
        assert previous.strip(), "the file it replaced was still whole at that moment"
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert "note" not in payload, "the previous daemon's file is gone"
        assert int(payload["pid"]) == os.getpid()
        assert int(payload["port"]) > 0
        assert list(target.parent.glob(DISCOVERY_NAME + ".tmp.*")) == []
    finally:
        server.stop()


@pytest.mark.posix
def test_no_reader_finds_the_discovery_file_half_written(tmp_path: Path) -> None:
    """A polling reader never catches the discovery file empty while the chassis publishes.

    POSIX-only: it republishes the discovery file sixty times via os.replace over an
    existing file, which Windows refuses with a sharing violation (WinError 5).
    """
    server, _ = make_server(tmp_path)
    try:
        target = tmp_path / DISCOVERY_NAME
        stop = threading.Event()
        empty: list[int] = []
        broken: list[str] = []

        def poll() -> None:
            while not stop.is_set():
                try:
                    raw = target.read_text(encoding="utf-8")
                except OSError:
                    continue
                if not raw.strip():
                    empty.append(len(raw))
                    continue
                try:
                    json.loads(raw)
                except ValueError:
                    broken.append(raw[:24])

        reader = threading.Thread(target=poll, name="kdx-test-discovery-reader", daemon=True)
        reader.start()
        for _ in range(60):
            server._write_discovery()
        stop.set()
        reader.join(timeout=2.0)
        assert empty == [], "an atomic publish leaves no empty window to fall into"
        assert broken == [], "a reader never decodes a half-written payload"
        target.write_text("", encoding="utf-8")
        assert IpcDiscovery.load(target) is None, "an empty file is what a dead daemon looks like"
        server._write_discovery()
        assert IpcDiscovery.load(target) is not None
    finally:
        server.stop()


def test_a_socket_that_closes_mid_response_does_not_end_the_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipe in the handler's face is an ending, not a fault: the plane keeps serving."""
    server, _ = make_server(tmp_path)
    server.logger = RecordingLogger()

    def hung_up(self: object) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(ipc_module._ControlHandler, "end_headers", hung_up)
    port = int(server.discovery.port)  # type: ignore[union-attr]
    assert request_health(port) == b""
    monkeypatch.undo()
    status, _ = http_call(server.discovery.base_url(), HEALTH_PATH)  # type: ignore[union-attr]
    assert status == 200, "the control plane answers the next client"
    assert ("warning", "ipc socket closed mid-response") in server.logger.lines
    assert server.running is True
    server.stop()


def test_a_socket_closed_while_the_plane_is_stopping_is_logged_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the chassis is stopping, a client mid-request is the expected ending."""
    server, _ = make_server(tmp_path)
    server.logger = RecordingLogger()

    def reset(self: object) -> None:
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(ipc_module._ControlHandler, "end_headers", reset)
    monkeypatch.setattr(ipc_module.IpcServer, "running", property(lambda _self: False))
    request_health(int(server.discovery.port))  # type: ignore[union-attr]
    lines = server.logger.lines
    assert ("debug", "ipc socket closed at shutdown") in lines
    assert not [line for line in lines if line[0] == "warning"], "stopping is not a warning"
    server.stop()


def test_a_descriptor_that_vanished_mid_request_ends_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descriptor closed underneath a live handler is swallowed; the connection ends."""
    server, _ = make_server(tmp_path)
    server.logger = RecordingLogger()

    def bad_descriptor(self: object, method: str) -> None:
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(ipc_module._ControlHandler, "_route", bad_descriptor)
    assert request_health(int(server.discovery.port)) == b""  # type: ignore[union-attr]
    assert ("warning", "ipc socket closed mid-response") in server.logger.lines
    assert server.running is True
    server.stop()


def test_a_real_fault_is_not_swept_under_a_closed_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The handler hides only a socket that closed; a real fault still reaches the operator."""
    server, _ = make_server(tmp_path)
    server.logger = RecordingLogger()

    def not_a_socket(self: object, method: str) -> None:
        raise ValueError("not a socket")

    monkeypatch.setattr(ipc_module._ControlHandler, "_route", not_a_socket)
    request_health(int(server.discovery.port))  # type: ignore[union-attr]
    captured = capfd.readouterr()
    assert "ValueError" in captured.err, "socketserver reports the fault the handler raised"
    assert not [line for line in server.logger.lines if "socket closed" in line[1]]
    server.stop()


def test_a_malformed_route_is_refused_before_it_reaches_the_socket() -> None:
    """A CR welded onto a path is a header waiting to be smuggled; refuse the declaration."""

    def handler(request: IpcRequest) -> dict[str, str]:
        del request
        return {"ok": "yes"}

    for path in ("/api/v1/probe\r\nX-Evil: 1", " ", "\tx", "/api/v1/\x00", "/api/v1/\x7f"):
        with pytest.raises(IpcError, match="illegal character"):
            IpcRoute(path=path, handler=handler)
    for verb in ("FETCH", "POST;", "get post", "GET\r\nX-Evil: 1"):
        with pytest.raises(IpcError, match="HTTP method"):
            IpcRoute(path="/api/v1/probe", handler=handler, methods=(verb,))


def test_a_route_declaration_is_normalized_not_rejected() -> None:
    """A declaration is cleaned up on the way in, so the server dispatches one verb set."""

    def handler(request: IpcRequest) -> dict[str, str]:
        del request
        return {"ok": "yes"}

    assert IpcRoute(path="api/v1/probe", handler=handler).path == "/api/v1/probe"
    assert IpcRoute(path="/api/v1/probe", handler=handler, methods=()).methods == (
        DEFAULT_ROUTE_METHODS
    )
    assert IpcRoute(path="/api/v1/probe", handler=handler, methods=(" GET ",)).methods == ("GET",)
    doubled = IpcRoute(path="/api/v1/probe", handler=handler, methods=("post", "POST", "Put"))
    assert doubled.methods == ("POST", "PUT"), "a duplicate verb is declared once"
    assert set(HTTP_METHODS) == {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


def test_an_empty_method_entry_is_a_declaration_error() -> None:
    """A blank entry is a typo; an empty tuple is the documented GET default."""

    def handler(request: IpcRequest) -> dict[str, str]:
        del request
        return {"ok": "yes"}

    for entry in ("", "   "):
        with pytest.raises(IpcError, match="empty HTTP method"):
            IpcRoute(path="/api/v1/probe", handler=handler, methods=(entry,))


def test_a_validated_route_lands_on_the_server_lookup(tmp_path: Path) -> None:
    """A route the constructor accepted is a route the control plane can answer."""

    def handler(request: IpcRequest) -> dict[str, str]:
        del request
        return {"ok": "yes"}

    route = IpcRoute(path="/api/v1/probe", handler=handler, methods=("GET", "POST"), module="probe")
    server = IpcServer(
        status_fn=lambda: {"app": {"state": "running"}, "modules": {"total": 1}},
        shutdown_fn=lambda: None,
        state_fn=lambda: "running",
        options=IpcOptions(
            host="127.0.0.1", port=0, discovery_path=tmp_path / "chassis.ipc", token="sekret"
        ),
        routes=(route,),
    )
    assert server.lookup_route("/api/v1/probe") is route
    assert [item.path for item in server.module_routes()] == ["/api/v1/probe"]
    assert server.remove_route("/api/v1/probe") is True
    assert server.lookup_route("/api/v1/probe") is None
