"""Custom content types and static directories on the control plane."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from korodexios_core.kernel.ipc import (
    CONTENT_HTML,
    CONTENT_JSON,
    CONTENT_OCTET_STREAM,
    CONTENT_PLAIN,
    STATIC_METHODS,
    IpcError,
    IpcOptions,
    IpcResponse,
    IpcRoute,
    IpcServer,
    IpcStaticMount,
    guess_content_type,
)

TOKEN_HEADER = "X-Kdx-Token"


def make_server(
    tmp_path: Path,
    *,
    routes: tuple[IpcRoute, ...] = (),
    static_dirs: tuple[IpcStaticMount, ...] = (),
    token: str = "sekret",
    auth: bool = True,
) -> IpcServer:
    """Start a control plane carrying the routes and mounts under test."""
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
        static_dirs=static_dirs,
    )
    server.start()
    return server


def http_call(
    base: str,
    route: str,
    *,
    token: str | None = "sekret",
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Issue one request and return (status, response headers, raw body)."""
    sent = {TOKEN_HEADER: token or ""}
    if headers:
        sent.update(headers)
    request = urllib.request.Request(base + route, headers=sent, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return (
                int(response.status),
                {str(k).lower(): str(v) for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        return (
            int(exc.code),
            {str(k).lower(): str(v) for k, v in exc.headers.items()},
            exc.read(),
        )


def write_ui(root: Path) -> Path:
    """Return a small static tree: an index, a stylesheet, a script, and bytes."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>UI</title>", encoding="utf-8")
    (root / "app.css").write_text("body { margin: 0 }", encoding="utf-8")
    (root / "app.js").write_text("console.log('kdx')", encoding="utf-8")
    (root / "deep").mkdir(exist_ok=True)
    # write_bytes keeps the CSV byte-exact: write_text would translate the
    # trailing newlines to CRLF on Windows and the served-bytes assertion would drift.
    (root / "deep" / "report.csv").write_bytes(b"a,b\n1,2\n")
    (root / "pixel.png").write_bytes(bytes.fromhex("89504e470d0a1a0a"))
    return root


@pytest.fixture()
def ui_dir(tmp_path: Path) -> Path:
    return write_ui(tmp_path / "ui")


def test_guess_content_type_labels_common_web_files() -> None:
    assert guess_content_type("index.html").startswith("text/html")
    assert guess_content_type("app.css").startswith("text/css")
    assert guess_content_type("app.js") in {
        "application/javascript",
        "text/javascript",
    }
    assert guess_content_type("data.json").startswith("application/json")
    # Windows' mimetypes reads the registry and answers 'application/vnd.ms-excel'
    # for '.csv'; the table pins the honest type on every platform.
    assert guess_content_type("report.csv") == "text/csv"
    assert guess_content_type("pixel.png").startswith("image/")
    assert guess_content_type("no-extension") == CONTENT_OCTET_STREAM
    assert guess_content_type("archive.unknownext") == CONTENT_OCTET_STREAM
    assert guess_content_type("style.CSS").startswith("text/css")


def test_response_shape_and_guards() -> None:
    plain = IpcResponse(b"hello")
    assert plain.length == 5
    assert plain.content_type == CONTENT_JSON
    assert plain.is_text is True
    assert plain.status == 200
    assert plain.header("content-length") == ""
    text = IpcResponse.text("hi", content_type=CONTENT_PLAIN)
    assert text.body == b"hi"
    html = IpcResponse.html("<p>x</p>", status=201, headers=(("X-Ui", "yes"),))
    assert html.status == 201
    assert html.header("X-UI") == "yes"
    assert html.content_type == CONTENT_HTML
    assert html.as_dict()["length"] == 8
    assert IpcResponse(body=bytearray(b"raw")).body == b"raw"
    assert IpcResponse(body=memoryview(b"raw")).body == b"raw"
    assert IpcResponse(body="text").body == b"text"
    with pytest.raises(IpcError):
        IpcResponse(body=42)  # type: ignore[arg-type]
    with pytest.raises(IpcError):
        IpcResponse(status=7)
    assert IpcResponse(content_type="   ").content_type == CONTENT_JSON


def test_file_response_reads_its_own_type(tmp_path: Path) -> None:
    target = tmp_path / "sheet.css"
    target.write_text("p { color: red }", encoding="utf-8")
    response = IpcResponse.file(target)
    assert response.body == b"p { color: red }"
    assert response.content_type.startswith("text/css")
    assert response.header("Cache-Control") == "no-cache"
    assert IpcResponse.file(target, cache_control="").header("Cache-Control") == ""
    with pytest.raises(IpcError):
        IpcResponse.file(tmp_path / "missing.css")


def test_route_content_type_declaration(ui_dir: Path) -> None:
    route = IpcRoute(path="/api/v1/page", handler=lambda request: "x", module="ui")
    assert route.content_type == CONTENT_JSON
    assert route.declared_content_type() == CONTENT_JSON
    assert route.binary_content_type() == CONTENT_OCTET_STREAM
    page = IpcRoute(
        path="/api/v1/page",
        handler=lambda request: "x",
        content_type="text/html",
        module="ui",
    )
    assert page.declared_content_type().startswith("text/html")
    css = IpcRoute(path="/api/v1/app.css", handler=lambda request: b"x", module="ui")
    assert css.binary_content_type().startswith("text/css")
    assert css.as_dict()["content_type"] == CONTENT_JSON
    assert page.as_dict()["content_type"] == "text/html"


def test_a_route_may_answer_html(tmp_path: Path) -> None:
    routes = (
        IpcRoute(
            path="/api/v1/page",
            handler=lambda request: IpcResponse.html("<!doctype html><h1>Ui</h1>"),
            module="ui",
            auth=False,
        ),
    )
    server = make_server(tmp_path, routes=routes)
    try:
        base = str(server.discovery.base_url())
        code, headers, body = http_call(base, "/api/v1/page", token=None)
        assert code == 200
        assert headers["content-type"].startswith("text/html")
        assert body.startswith(b"<!doctype html>")
    finally:
        server.stop()


def test_bytes_and_str_bodies_keep_their_own_meaning(tmp_path: Path) -> None:
    routes = (
        IpcRoute(
            path="/api/v1/blob",
            handler=lambda request: bytes([0, 1, 2, 255]),
            module="ui",
            auth=False,
        ),
        IpcRoute(
            path="/api/v1/plain",
            handler=lambda request: "just text",
            content_type="text/plain",
            module="ui",
            auth=False,
        ),
        IpcRoute(
            path="/api/v1/report.csv",
            handler=lambda request: b"a,b\n1,2\n",
            module="ui",
            auth=False,
        ),
        IpcRoute(
            path="/api/v1/legacy",
            handler=lambda request: "plain old string",
            module="ui",
            auth=False,
        ),
        IpcRoute(
            path="/api/v1/created",
            handler=lambda request: IpcResponse.text(
                "made", status=201, headers=(("X-Kind", "new"),)
            ),
            module="ui",
            auth=False,
        ),
    )
    server = make_server(tmp_path, routes=routes)
    try:
        base = str(server.discovery.base_url())
        code, headers, body = http_call(base, "/api/v1/blob", token=None)
        assert (code, body) == (200, bytes([0, 1, 2, 255]))
        assert headers["content-type"] == CONTENT_OCTET_STREAM
        code, headers, body = http_call(base, "/api/v1/plain", token=None)
        assert body == b"just text"
        assert headers["content-type"].startswith("text/plain")
        code, headers, body = http_call(base, "/api/v1/report.csv", token=None)
        assert headers["content-type"].startswith("text/csv")
        assert body == b"a,b\n1,2\n"
        code, headers, body = http_call(base, "/api/v1/legacy", token=None)
        assert headers["content-type"] == CONTENT_JSON
        assert json.loads(body) == "plain old string"
        code, headers, body = http_call(base, "/api/v1/created", token=None)
        assert code == 201
        assert headers["x-kind"] == "new"
        assert body == b"made"
    finally:
        server.stop()


def test_a_static_directory_is_served_by_the_control_plane(tmp_path: Path, ui_dir: Path) -> None:
    server = make_server(
        tmp_path, static_dirs=(IpcStaticMount("/ui", ui_dir, module="ui"),)
    )
    try:
        base = str(server.discovery.base_url())
        code, headers, body = http_call(base, "/ui/app.css", token=None)
        assert code == 200
        assert headers["content-type"].startswith("text/css")
        assert body == b"body { margin: 0 }"
        assert headers["cache-control"] == "no-cache"
        assert headers["etag"].startswith('W/"')
        code, headers, body = http_call(base, "/ui/app.js", token=None)
        assert headers["content-type"] in {"application/javascript", "text/javascript"}
        code, headers, body = http_call(base, "/ui/", token=None)
        assert code == 200
        assert headers["content-type"].startswith("text/html")
        assert body.startswith(b"<!doctype html>")
        code, headers, body = http_call(base, "/ui/deep/report.csv", token=None)
        assert body == b"a,b\n1,2\n"
        code, headers, body = http_call(base, "/ui/pixel.png", token=None)
        assert headers["content-type"] == "image/png"
        assert body == bytes.fromhex("89504e470d0a1a0a")
    finally:
        server.stop()


def test_static_head_conditional_and_error_semantics(tmp_path: Path, ui_dir: Path) -> None:
    server = make_server(tmp_path, static_dirs=(IpcStaticMount("/ui", ui_dir),))
    try:
        base = str(server.discovery.base_url())
        _code, headers, _body = http_call(base, "/ui/app.css", token=None)
        etag = headers["etag"]
        code, head_headers, raw = http_call(base, "/ui/app.css", token=None, method="HEAD")
        assert code == 200
        assert raw == b""
        assert int(head_headers["content-length"]) == len(b"body { margin: 0 }")
        code, _headers, body = http_call(
            base, "/ui/app.css", token=None, headers={"If-None-Match": etag}
        )
        assert code == 304
        assert body == b""
        code, _headers, body = http_call(base, "/ui/missing.css", token=None)
        assert code == 404
        assert b"no such file" in body
        code, _headers, body = http_call(base, "/ui/../kdx.yml", token=None)
        assert code in {403, 404}
        code, _headers, body = http_call(
            base, "/ui/%%2e%%2e%%2f%%2e%%2e%%2fsecret", token=None
        )
        assert code in {200, 403, 404}
        code, _headers, body = http_call(base, "/ui/app.css", token=None, method="POST")
        assert code == 405
        assert "GET" in str(body)
        code, _headers, body = http_call(base, "/other/app.css", token=None)
        assert code == 404
    finally:
        server.stop()


def test_a_static_mount_may_require_the_shared_token(tmp_path: Path, ui_dir: Path) -> None:
    server = make_server(
        tmp_path, static_dirs=(IpcStaticMount("/ui", ui_dir, auth=True),)
    )
    try:
        base = str(server.discovery.base_url())
        code, _headers, _body = http_call(base, "/ui/app.css", token=None)
        assert code == 401
        code, _headers, body = http_call(base, "/ui/app.css", token="sekret")
        assert code == 200
        assert body.startswith(b"body")
    finally:
        server.stop()


def test_static_mount_shape_and_guards(ui_dir: Path) -> None:
    mount = IpcStaticMount("ui/", ui_dir, index_names=("home.html",), module="ui")
    assert mount.prefix == "/ui"
    assert mount.allows("head") is True
    assert mount.allows("POST") is False
    assert mount.matches("/ui/app.css") is True
    assert mount.matches("/ui") is True
    assert mount.matches("/uiise") is False
    assert mount.relative("/ui/deep/report.csv") == "deep/report.csv"
    assert mount.relative("/ui") == ""
    assert mount.resolve("app.css") == ui_dir / "app.css"
    assert mount.resolve("sub/app.js") == ui_dir / "sub" / "app.js"
    assert mount.index_names == ("home.html",)
    assert mount.as_dict()["index_names"] == ["home.html"]
    assert mount.as_dict()["route_prefix"] == "/ui"
    assert mount.as_dict()["directory"] == str(ui_dir)
    assert mount.as_dict()["methods"] == ["GET", "HEAD"]
    assert IpcStaticMount("/ui", ui_dir, index_names=()).index_names == ("index.html",)
    assert STATIC_METHODS == ("GET", "HEAD")
    with pytest.raises(IpcError):
        IpcStaticMount("", ui_dir)
    with pytest.raises(IpcError):
        IpcStaticMount("/api/v1/status", ui_dir)
    with pytest.raises(IpcError):
        IpcStaticMount("/ui", ui_dir / "missing-directory")
    outside = ui_dir.parent / "escape.css"
    outside.write_text("x {}", encoding="utf-8")
    assert mount.resolve("../escape.css") is None


def test_static_mounts_are_live_and_replaceable(tmp_path: Path, ui_dir: Path) -> None:
    server = make_server(tmp_path)
    try:
        base = str(server.discovery.base_url())
        code, _headers, _body = http_call(base, "/ui/app.css", token=None)
        assert code == 404
        mount = server.register_static_dir("/ui", ui_dir, module="ui")
        assert isinstance(mount, IpcStaticMount)
        code, _headers, body = http_call(base, "/ui/app.css", token=None)
        assert code == 200
        assert body.startswith(b"body")
        assert server.lookup_static("/ui/app.css") == (mount, "app.css")
        assert server.lookup_static("/elsewhere") is None
        assert [item.prefix for item in server.static_mounts()] == ["/ui"]
        moved = tmp_path / "moved"
        moved.mkdir()
        (moved / "app.css").write_text("moved {}", encoding="utf-8")
        server.register_static_dir("/ui", moved)
        code, _headers, body = http_call(base, "/ui/app.css", token=None)
        assert body == b"moved {}"
        assert server.unregister_static_dir("/ui") is True
        assert server.unregister_static_dir("/ui") is False
        code, _headers, _body = http_call(base, "/ui/app.css", token=None)
        assert code == 404
        with pytest.raises(IpcError):
            server.register_static_dir("/api/v1/status", ui_dir)
    finally:
        server.stop()


def test_reserved_prefixes_and_route_collisions(tmp_path: Path, ui_dir: Path) -> None:
    server = make_server(tmp_path, routes=())
    try:
        server.add_route(
            IpcRoute(path="/api/v1/widget", handler=lambda request: {}, module="ui")
        )
        with pytest.raises(IpcError):
            server.add_static_dir(IpcStaticMount("/api/v1/widget", ui_dir))
        assert server.add_static_dir(IpcStaticMount("/ui", ui_dir)) is server
    finally:
        server.stop()


def test_the_contract_documents_types_and_mounts(tmp_path: Path, ui_dir: Path) -> None:
    server = make_server(
        tmp_path,
        routes=(
            IpcRoute(
                path="/api/v1/page",
                handler=lambda request: "x",
                content_type="text/html",
                module="ui",
            ),
        ),
        static_dirs=(IpcStaticMount("/ui", ui_dir, module="ui"),),
    )
    try:
        contract = server.api_contract()
        assert contract["static_mounts"][0]["route_prefix"] == "/ui"
        by_path = {row["path"]: row for row in contract["routes"]}
        assert "/api/v1/page" not in by_path  # core routes only, as ever
        assert by_path["/api/v1/status"]["content_type"] == CONTENT_JSON
        modules = {row["path"]: row for row in contract["module_routes"]}
        assert modules["/api/v1/page"]["content_type"] == "text/html"
        assert contract["schema"] == 1
        assert "static_mounts" in by_path["/api/v1/schema"]["fields"]
        payload = server.as_dict()
        assert payload["static"] == ["/ui"]
        assert payload["static_mounts"][0]["module"] == "ui"
    finally:
        server.stop()


def test_the_control_plane_still_answers_json(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    try:
        base = str(server.discovery.base_url())
        code, headers, body = http_call(base, "/api/v1/status", token="sekret")
        assert code == 200
        assert headers["content-type"] == CONTENT_JSON
        assert json.loads(body)["app"]["state"] == "running"
    finally:
        server.stop()
