"""Declarable control-plane routes, the RPC delegation path, and reload over IPC."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from korodexios_core.kernel.ipc import (
    RELOAD_PATH,
    ROUTES,
    RPC_PATH,
    STATUS_PATH,
    TOKEN_HEADER,
    IpcDiscovery,
    IpcError,
    IpcOptions,
    IpcRequest,
    IpcRoute,
    IpcServer,
    normalize_route_path,
)


def make_server(
    tmp_path: Path,
    *,
    routes: tuple[IpcRoute, ...] = (),
    rpc_fn: object = None,
    reload_fn: object = None,
    token: str = "sekret",
    auth: bool = True,
) -> IpcServer:
    """Start a control plane carrying the declarative collaborators under test."""
    server = IpcServer(
        status_fn=lambda: {"app": {"state": "running"}, "modules": {"total": 0}},
        shutdown_fn=lambda: None,
        state_fn=lambda: "running",
        options=IpcOptions(
            host="127.0.0.1",
            port=0,
            discovery_path=tmp_path / "chassis.ipc",
            token=token,
            auth=auth,
        ),
        routes=routes,
        rpc_fn=rpc_fn,  # type: ignore[arg-type]
        reload_fn=reload_fn,  # type: ignore[arg-type]
    )
    server.start()
    return server


def http_call(
    base: str,
    route: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: object = None,
) -> tuple[int, object]:
    """Issue one control-plane request and return (status, decoded body)."""
    data = None
    headers = {TOKEN_HEADER: token or ""}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + route, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return int(response.status), (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return int(exc.code), (json.loads(raw) if raw else {})


def probe_routes() -> tuple[IpcRoute, ...]:
    """Return one authenticated and one public module route."""
    return (
        IpcRoute(
            path="/api/v1/probe",
            handler=lambda request: {"echo": request.argument("q")},
            module="probe",
            summary="echo a query argument",
        ),
        IpcRoute(
            path="/api/v1/open",
            handler=lambda request: {"ok": True},
            methods=("GET", "POST"),
            auth=False,
            module="probe",
        ),
    )


def test_normalize_route_path_accepts_bare_names() -> None:
    assert normalize_route_path("probe") == "/probe"
    assert normalize_route_path("/probe") == "/probe"
    assert normalize_route_path("/api/v1/probe/") == "/api/v1/probe"
    assert normalize_route_path("") == "/"


def test_route_shape_and_reserved_paths() -> None:
    route = probe_routes()[0]
    assert route.allows("get") is True
    assert route.allows("POST") is False
    payload = route.as_dict()
    assert payload["path"] == "/api/v1/probe"
    assert payload["methods"] == ["GET"]
    assert payload["module"] == "probe"
    assert payload["summary"] == "echo a query argument"
    for reserved in ROUTES:
        with pytest.raises(IpcError) as captured:
            IpcRoute(path=reserved, handler=lambda request: {}, module="probe")
        assert "reserved" in str(captured.value)
        assert captured.value.hint
    assert IpcRoute(path="metrics", handler=lambda request: {}).path == "/metrics"
    assert IpcRoute(
        path="/api/v1/x", handler=lambda request: {}, methods=("get", "post")
    ).methods == ("GET", "POST")
    assert IpcRoute(path="/api/v1/x", handler=lambda request: {}, methods=()).methods == ("GET",)


def test_request_argument_precedence() -> None:
    request = IpcRequest(
        method="POST",
        path="/api/v1/probe",
        query={"q": ["first", "second"]},
        payload={"q": "body", "other": 1},
        headers={"X-Test": "yes"},
    )
    assert request.argument("q") == "first"
    assert request.argument("other") == 1
    assert request.argument("missing", default="fallback") == "fallback"
    assert request.headers["X-Test"] == "yes"
    bare = IpcRequest(method="GET", path="/x", query={})
    assert bare.argument("q", default=None) is None


def test_module_routes_answer_and_gate_on_token(tmp_path: Path) -> None:
    server = make_server(tmp_path, routes=probe_routes())
    try:
        base = str(server.discovery.base_url())
        code, payload = http_call(base, "/api/v1/probe?q=hello", token="sekret")
        assert code == 200
        assert payload == {"echo": "hello"}
        code, _ = http_call(base, "/api/v1/probe", token="wrong")
        assert code == 401
        code, _ = http_call(base, "/api/v1/probe")
        assert code == 401
        code, payload = http_call(base, "/api/v1/open")
        assert code == 200 and payload == {"ok": True}
        code, payload = http_call(base, "/api/v1/open", method="POST", payload={"a": 1})
        assert code == 200
        code, payload = http_call(base, "/api/v1/probe", method="POST", token="sekret")
        assert code == 405
        assert "GET" in str(payload)
        code, payload = http_call(base, "/api/v1/nothing")
        assert code == 404
    finally:
        server.stop()


def test_route_handler_errors_are_reported(tmp_path: Path) -> None:
    def boom(request: IpcRequest) -> dict[str, str]:
        raise RuntimeError("route exploded")

    def coded(request: IpcRequest) -> tuple[int, dict[str, str]]:
        return 202, {"accepted": "yes"}

    server = make_server(
        tmp_path,
        routes=(
            IpcRoute(path="/api/v1/boom", handler=boom, module="probe"),
            IpcRoute(path="/api/v1/coded", handler=coded, module="probe"),
        ),
    )
    try:
        base = str(server.discovery.base_url())
        code, payload = http_call(base, "/api/v1/boom", token="sekret")
        assert code == 500
        assert "route exploded" in str(payload)
        assert server.as_dict()["errors"] >= 1
        code, payload = http_call(base, "/api/v1/coded", token="sekret")
        assert code == 202 and payload == {"accepted": "yes"}
    finally:
        server.stop()


def test_route_registration_is_live(tmp_path: Path) -> None:
    server = make_server(tmp_path, routes=probe_routes())
    try:
        assert [route.path for route in server.module_routes()] == [
            "/api/v1/open",
            "/api/v1/probe",
        ]
        assert server.lookup_route("/api/v1/probe") is not None
        assert server.lookup_route("/api/v1/missing") is None
        added = IpcRoute(path="/api/v1/late", handler=lambda request: {"late": True}, module="x")
        assert server.add_route(added) is server
        assert server.lookup_route("/api/v1/late") is not None
        assert server.remove_route("/api/v1/late") is True
        assert server.remove_route("/api/v1/late") is False
        routes_payload = server.as_dict()["routes"]
        assert "/api/v1/probe" in [item["path"] for item in routes_payload]
        contract = server.api_contract()
        methods = {item["path"]: item["method"] for item in contract["routes"]}
        assert methods[RELOAD_PATH] == "POST"
        assert methods[RPC_PATH] == "POST"
        assert "/api/v1/probe" in [item["path"] for item in contract["module_routes"]]
    finally:
        server.stop()


def test_rpc_route_delegates_to_the_chassis(tmp_path: Path) -> None:
    def rpc(command: str, args: object) -> dict[str, object]:
        return {"command": command, "args": dict(args or {})}

    rpc_server = make_server(tmp_path, rpc_fn=rpc)
    server = rpc_server
    try:
        base = str(server.discovery.base_url())
        code, payload = http_call(
            base, RPC_PATH, method="POST", token="sekret", payload={"command": "heartbeat status"}
        )
        assert code == 200
        assert payload["ok"] is True
        assert payload["command"] == "heartbeat status"
        assert payload["result"]["args"] == {}
        code, payload = http_call(
            base,
            RPC_PATH,
            method="POST",
            token="sekret",
            payload={"command": "nope", "args": {"lines": 5}},
        )
        assert code == 200
        assert payload["result"]["args"] == {"lines": 5}
        code, payload = http_call(base, RPC_PATH, method="POST", token="sekret", payload={})
        assert code == 400
        assert "command" in str(payload)
        code, _ = http_call(base, RPC_PATH, token="sekret")
        assert code == 405
        assert rpc_server.handle_rpc("not-a-mapping")[0] == 400
    finally:
        server.stop()


def test_rpc_errors_map_to_http_status(tmp_path: Path) -> None:
    def lookup(command: str, args: object) -> dict[str, str]:
        raise LookupError("no module command named 'nope'")

    def bad_type(command: str, args: object) -> dict[str, str]:
        raise TypeError("handler wants a different namespace")

    def broken(command: str, args: object) -> dict[str, str]:
        raise RuntimeError("handler exploded")

    for handler, status in ((lookup, 404), (bad_type, 400), (broken, 500)):
        server = make_server(tmp_path, rpc_fn=handler)
        try:
            code, payload = server.handle_rpc({"command": "x"})
            assert code == status, (handler, code)
            assert payload["ok"] is False
            assert "error" in payload
        finally:
            server.stop()

    plain = make_server(tmp_path)
    try:
        assert plain.handle_rpc({"command": "x"})[0] == 501
        assert plain.handle_rpc("not-a-mapping")[0] == 501  # no rpc hook wins the check
    finally:
        plain.stop()


def test_reload_route_over_http(tmp_path: Path) -> None:
    calls = {"count": 0}

    def reload_fn() -> dict[str, int]:
        calls["count"] += 1
        return {"reloaded": True, "reloads": calls["count"]}

    server = make_server(tmp_path, reload_fn=reload_fn)
    try:
        base = str(server.discovery.base_url())
        code, payload = http_call(base, RELOAD_PATH, method="POST", token="sekret")
        assert code == 200
        assert payload == {"reloaded": True, "reloads": 1}
        code, _ = http_call(base, RELOAD_PATH, method="POST")
        assert code == 401
        assert server.request_reload()["reloads"] == 2
    finally:
        server.stop()

    quiet = make_server(tmp_path)
    try:
        assert "no reload hook" in quiet.request_reload()["reason"]
        assert quiet.handle_rpc({"command": "x"})[0] == 501
    finally:
        quiet.stop()


def test_token_compare_is_constant_time_and_optional(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    try:
        assert server.token_matches("sekret") is True
        assert server.token_matches("wrong") is False
        assert server.token_matches(None) is False
        assert server.token_matches("") is False
        assert server.requires_token(STATUS_PATH) is True
        assert server.requires_token(RPC_PATH) is True
        assert server.requires_token(RELOAD_PATH) is True
        assert server.requires_token("/health") is False
    finally:
        server.stop()

    open_server = make_server(tmp_path, auth=False)
    try:
        assert open_server.requires_token(RPC_PATH) is False
        assert open_server.token_matches(None) is False  # the gate, not the compare
    finally:
        open_server.stop()


def test_discovery_client_calls_routes(tmp_path: Path) -> None:
    server = make_server(tmp_path, routes=probe_routes(), rpc_fn=lambda c, a: {"command": c})
    try:
        discovery = IpcDiscovery.load(tmp_path / "chassis.ipc")
        assert discovery is not None
        code, payload = discovery.request("/api/v1/probe", timeout=5)
        assert code == 200 and payload == {"echo": None}
        code, payload = discovery.call("heartbeat status", {"lines": 2}, timeout=5)
        assert code == 200 and payload["command"] == "heartbeat status"
        code, payload = discovery.request(RELOAD_PATH, method="POST", timeout=5)
        assert code == 200
        assert payload["reloaded"] is False
    finally:
        server.stop()
