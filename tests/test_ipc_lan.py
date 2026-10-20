"""LAN accessibility: host binding, optional authentication, static tokens, remote status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.kernel.ipc import (
    DISCOVERY_NAME,
    SHUTDOWN_PATH,
    STATUS_PATH,
    TOKEN_HEADER,
    IpcDiscovery,
    IpcError,
    IpcOptions,
    IpcServer,
    is_loopback_host,
    is_wildcard_host,
    new_token,
)
from korodexios_core.paths import AppPaths

from .test_ipc import base_args, http_call, ipc_chassis


def lan_server(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    token: str = "lan-key-static",
    auth: bool = True,
    allow_anonymous_shutdown: bool = False,
) -> tuple[IpcServer, dict[str, int]]:
    """Start a control plane with an explicit authentication policy."""
    calls = {"shutdown": 0}
    server = IpcServer(
        status_fn=lambda: {"app": {"state": "running"}, "modules": {"total": 0}},
        shutdown_fn=lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1),
        state_fn=lambda: "running",
        options=IpcOptions(
            host=host,
            port=0,
            discovery_path=tmp_path / DISCOVERY_NAME,
            token=token,
            token_source="static" if token else "ephemeral",
            auth=auth,
            allow_anonymous_shutdown=allow_anonymous_shutdown,
        ),
    )
    server.start()
    return server, calls


def test_host_classification_is_explicit() -> None:
    assert is_wildcard_host("0.0.0.0") is True
    assert is_wildcard_host("127.0.0.1") is False
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("192.168.1.50") is False


def test_new_token_is_ephemeral_and_distinct() -> None:
    first, second = new_token(), new_token()
    assert len(first) >= 16
    assert first != second


def test_auth_disabled_reads_status_without_a_token(tmp_path: Path) -> None:
    server, _ = lan_server(tmp_path, auth=False)
    base = str(server.discovery and server.discovery.base_url())
    try:
        code, payload = http_call(base, STATUS_PATH)
        assert code == 200
        assert payload["app"]["state"] == "running"  # type: ignore[index]
        assert payload["ipc"]["auth"] is False  # type: ignore[index]
        assert payload["ipc"]["token_source"] == "static"  # type: ignore[index]
        assert '"token":' not in json.dumps(payload)
        assert '"token_source": "static"' in json.dumps(payload)
    finally:
        server.stop()


def test_shutdown_stays_authenticated_when_reads_are_anonymous(tmp_path: Path) -> None:
    server, calls = lan_server(tmp_path, auth=False, allow_anonymous_shutdown=False)
    base = str(server.discovery and server.discovery.base_url())
    try:
        assert http_call(base, STATUS_PATH)[0] == 200
        code, payload = http_call(base, SHUTDOWN_PATH, method="POST")
        assert code == 401
        assert "hint" in payload  # type: ignore[index]
        assert calls["shutdown"] == 0
        assert http_call(base, SHUTDOWN_PATH, token="lan-key-static", method="POST")[0] == 200
        assert calls["shutdown"] == 1
    finally:
        server.stop()


def test_anonymous_shutdown_when_configured(tmp_path: Path) -> None:
    server, calls = lan_server(tmp_path, auth=False, token="", allow_anonymous_shutdown=True)
    base = str(server.discovery and server.discovery.base_url())
    try:
        assert server.token_source == "ephemeral"
        assert http_call(base, SHUTDOWN_PATH, method="POST")[0] == 200
        assert calls["shutdown"] == 1
    finally:
        server.stop()


def test_static_token_is_announced_in_the_discovery_file(tmp_path: Path) -> None:
    server, _ = lan_server(tmp_path, token="lan-key-static")
    target = tmp_path / DISCOVERY_NAME
    try:
        recorded = json.loads(target.read_text(encoding="utf-8"))
        assert recorded["token"] == "lan-key-static"
        assert server.as_dict()["token_source"] == "static"
        assert "token" not in server.as_dict()
        assert oct(target.stat().st_mode & 0o777) == "0o600"
    finally:
        server.stop()


def test_remote_discovery_parses_every_target_shape() -> None:
    full = IpcDiscovery.remote("http://192.168.1.50:8123")
    assert (full.host, full.port, full.scheme) == ("192.168.1.50", 8123, "http")
    assert full.base_url() == "http://192.168.1.50:8123"
    bare = IpcDiscovery.remote("192.168.1.50", default_port=9123)
    assert (bare.host, bare.port) == ("192.168.1.50", 9123)
    with_port = IpcDiscovery.remote("10.8.0.9:8123")
    assert (with_port.host, with_port.port) == ("10.8.0.9", 8123)
    assert bare.is_process_alive() is False  # a remote target has no local pid
    with pytest.raises(IpcError, match="no endpoint given"):
        IpcDiscovery.remote("   ")
    with pytest.raises(IpcError, match="cannot read a host"):
        IpcDiscovery.remote("http://:8123")


def test_client_sends_no_token_header_when_the_token_is_empty(tmp_path: Path) -> None:
    server, _ = lan_server(tmp_path, auth=False, token="")
    url = server.discovery and server.discovery.base_url()
    remote = IpcDiscovery.remote(str(url), token="")
    try:
        code, payload = remote.request(STATUS_PATH)
        assert code == 200
        assert payload["app"]["state"] == "running"  # type: ignore[index]
    finally:
        server.stop()


def test_chassis_binds_a_wildcard_host_without_complaint(project_root) -> None:
    paths = AppPaths.discover(root=str(project_root), environ={})
    chassis = Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            cli_overrides={
                "core.log.file_enabled": False,
                "core.ipc.enabled": True,
                "core.ipc.host": "0.0.0.0",
                "core.ipc.port": 0,
                "core.ipc.token": "lan-key-static",
                "core.ipc.auth": False,
            },
            install_signals=False,
        )
    )
    chassis.initialize()
    chassis.start()
    try:
        assert chassis.ipc is not None
        assert chassis.ipc.running is True
        assert chassis.ipc.bound_host == "0.0.0.0"
        assert chassis.ipc.lan_accessible is True
        assert chassis.ipc.token_source == "static"
        report = chassis.health_report()["ipc"]
        assert report["bound_host"] == "0.0.0.0"
        assert report["auth"] is False
        recorded = json.loads(
            (chassis.paths.state_dir / DISCOVERY_NAME).read_text(encoding="utf-8")
        )
        assert recorded["host"] == "127.0.0.1"
        assert recorded["token"] == "lan-key-static"
    finally:
        chassis.shutdown()


def test_status_command_queries_a_remote_target(capsys, project_root) -> None:
    chassis = ipc_chassis(project_root)
    chassis.initialize()
    chassis.start()
    try:
        discovery = chassis.ipc.discovery  # type: ignore[union-attr]
        target = f"http://127.0.0.1:{discovery.port}"
        args = [
            *base_args(project_root),
            "status",
            "--target",
            target,
            "--token",
            discovery.token,
        ]
        assert main(args) == int(ExitCode.SUCCESS)
        human = capsys.readouterr().out
        assert "Chassis" in human
        assert discovery.base_url() in human
        args_json = [
            *base_args(project_root),
            "status",
            "--target",
            target,
            "--token",
            discovery.token,
            "--json",
        ]
        assert main(args_json) == int(ExitCode.SUCCESS)
        payload = json.loads(capsys.readouterr().out)
        assert payload["app"]["state"] == "running"
        assert payload["ipc"]["port"] == discovery.port
    finally:
        chassis.shutdown()


def test_status_command_explains_a_missing_remote_token(capsys, project_root) -> None:
    chassis = ipc_chassis(project_root)
    chassis.initialize()
    chassis.start()
    try:
        discovery = chassis.ipc.discovery  # type: ignore[union-attr]
        target = f"http://127.0.0.1:{discovery.port}"
        args = [*base_args(project_root), "status", "--target", target]
        assert main(args) == int(ExitCode.ERROR)
        captured = capsys.readouterr()
        assert "requires a token" in captured.out
        assert "KDX_CORE__IPC__TOKEN" in captured.out
    finally:
        chassis.shutdown()


def test_status_command_rejects_a_malformed_target(capsys, project_root) -> None:
    args = [*base_args(project_root), "status", "--target", "http://:8123"]
    assert main(args) == int(ExitCode.ERROR)
    captured = capsys.readouterr()
    assert "cannot read a host" in captured.out
    assert "--target http://host:port" in captured.out


def test_status_command_accepts_host_and_port(capsys, project_root) -> None:
    chassis = ipc_chassis(project_root)
    chassis.initialize()
    chassis.start()
    try:
        discovery = chassis.ipc.discovery  # type: ignore[union-attr]
        args = [
            *base_args(project_root),
            "status",
            "--host",
            "127.0.0.1",
            "--port",
            str(discovery.port),
            "--token",
            discovery.token,
            "--json",
        ]
        assert main(args) == int(ExitCode.SUCCESS)
        payload = json.loads(capsys.readouterr().out)
        assert payload["ipc"]["port"] == discovery.port
    finally:
        chassis.shutdown()


def test_token_header_name_is_stable() -> None:
    assert TOKEN_HEADER == "X-Korodex-Token"
    assert DISCOVERY_NAME == "chassis.ipc"
