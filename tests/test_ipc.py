"""IPC control plane tests: endpoints, token gate, discovery file, and 'kdx status'."""

from __future__ import annotations

import inspect
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import IPC_SCHEMA_VERSION, ExitCode
from korodexios_core.kernel import ipc as ipc_module
from korodexios_core.kernel.chassis import Chassis, ChassisOptions, ChassisState
from korodexios_core.kernel.ipc import (
    DISCOVERY_NAME,
    HEALTH_PATH,
    IS_LINUX,
    ROUTES,
    SCHEMA_PATH,
    SHUTDOWN_PATH,
    STATUS_PATH,
    TOKEN_HEADER,
    IpcOptions,
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
    (ui / "app.js").write_text("// app\n", encoding="utf-8")
    (ui / "sub" / "app.js").write_text("// nested\n", encoding="utf-8")
    (ui / "index.html").write_text("<html><body>ui</body></html>\n", encoding="utf-8")
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
    """A status snapshot reads memory without touching the platform module."""
    memory = process_memory()
    assert memory["source"] in {"proc/statm", "getrusage"}
    assert isinstance(memory["rss_bytes"], int)
    assert memory["rss"] != "-"
