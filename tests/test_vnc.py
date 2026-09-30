"""The noVNC live view under /vnc, relayed to real local HTTP and WebSocket servers."""

import base64
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import ServerConnection, serve

from app.vnc_proxy import is_allowed_asset_path
from tests.app_client import build_client
from tests.fakes import TEST_API_KEY

LOOPBACK = "127.0.0.1"
BASIC_AUTH = ("any-user", TEST_API_KEY)
VIEWER_HTML = b"<html>noVNC viewer</html>"
UI_SCRIPT = b"export const ui = 1;"
SAME_ORIGIN = {"Origin": "http://testserver"}
FOREIGN_ORIGIN = {"Origin": "https://evil.example"}
WEBSOCKET_PATH = "/vnc/websockify"
VIEWER_PATH = "/vnc/app/vnc.html"
POLICY_VIOLATION = 1008
INTERNAL_ERROR = 1011
FRAME = b"\x00\x01rfb-frame"


class NoVncHandler(BaseHTTPRequestHandler):
    """Stands in for websockify's static web server."""

    routes = {
        "/vnc.html": (200, "text/html", VIEWER_HTML),
        "/app/ui.js": (200, "text/javascript", UI_SCRIPT),
        "/broken": (500, "text/plain", b"boom"),
    }
    requested: list[str] = []

    def do_GET(self):
        NoVncHandler.requested.append(self.path)
        path = self.path.split("?")[0]
        status, content_type, body = self.routes.get(path, (404, "text/plain", b""))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def echo(connection: ServerConnection) -> None:
    for message in connection:
        connection.send(message)


def basic_header(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def static_port():
    server = ThreadingHTTPServer((LOOPBACK, 0), NoVncHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def websocket_port():
    server = serve(echo, LOOPBACK, 0, subprotocols=["binary"])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.socket.getsockname()[1]
    server.shutdown()


def vnc_client(port: int, **env: str):
    return build_client(ENABLE_VNC="true", VNC_PORT=str(port), **env)


@pytest.mark.parametrize("path", ["/vnc", VIEWER_PATH])
def test_vnc_routes_are_hidden_when_disabled(path):
    with build_client() as client:
        response = client.get(path, auth=BASIC_AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_vnc_websocket_is_refused_when_disabled():
    with build_client() as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WEBSOCKET_PATH, headers=SAME_ORIGIN):
            pass


@pytest.mark.parametrize("path", ["/novnc/vnc.html", "/novnc/websockify", "/novnc/"])
def test_novnc_prefix_is_not_served(static_port, path):
    with vnc_client(static_port) as client:
        response = client.get(path, auth=BASIC_AUTH)

    assert response.status_code == 404


def test_vnc_page_requires_basic_auth(static_port):
    with vnc_client(static_port) as client:
        response = client.get("/vnc")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_vnc_page_frames_the_viewer_from_the_same_origin(static_port):
    with vnc_client(static_port) as client:
        response = client.get("/vnc", auth=BASIC_AUTH)

    expected_src = "/vnc/app/vnc.html?autoconnect=true&amp;resize=scale&amp;path=/vnc/websockify"
    assert response.status_code == 200
    assert f'src="{expected_src}"' in response.text


def test_vnc_page_sets_a_restricted_session_cookie(static_port):
    with vnc_client(static_port) as client:
        cookie = client.get("/vnc", auth=BASIC_AUTH).headers["set-cookie"]

    assert cookie.startswith("vnc_session=")
    assert "HttpOnly" in cookie
    assert "Path=/vnc" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=28800" in cookie
    assert "Secure" not in cookie


def test_session_cookie_is_secure_behind_a_tls_proxy(static_port):
    with vnc_client(static_port) as client:
        headers = {"X-Forwarded-Proto": "https"}
        cookie = client.get("/vnc", auth=BASIC_AUTH, headers=headers).headers["set-cookie"]

    assert "Secure" in cookie


def test_viewer_files_require_credentials(static_port):
    with vnc_client(static_port) as client:
        response = client.get(VIEWER_PATH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_viewer_files_are_relayed_with_the_session_cookie(static_port):
    with vnc_client(static_port) as client:
        client.get("/vnc", auth=BASIC_AUTH)
        response = client.get("/vnc/app/app/ui.js")

    assert response.status_code == 200
    assert response.content == UI_SCRIPT
    assert response.headers["content-type"].startswith("text/javascript")


def test_viewer_files_are_relayed_with_basic_credentials(static_port):
    with vnc_client(static_port) as client:
        response = client.get(VIEWER_PATH, auth=BASIC_AUTH)

    assert response.content == VIEWER_HTML


def test_forged_session_cookie_is_rejected(static_port):
    with vnc_client(static_port) as client:
        client.cookies.set("vnc_session", "9999999999.forged", path="/vnc")
        response = client.get(VIEWER_PATH)

    assert response.status_code == 401


def test_viewer_query_is_forwarded_url_encoded(static_port):
    with vnc_client(static_port) as client:
        client.get(VIEWER_PATH, params={"autoconnect": "true", "x": "a b&c"}, auth=BASIC_AUTH)

    assert NoVncHandler.requested[-1] == "/vnc.html?autoconnect=true&x=a+b%26c"


@pytest.mark.parametrize(
    "path",
    [
        "/vnc/app/%2e%2e/secret",
        "/vnc/app/app%2F..%2F..%2Fetc/passwd",
        "/vnc/app/%252e%252e/secret",
        "/vnc/app/http:%2F%2Fevil.example/x",
        "/vnc/app//etc/passwd",
    ],
)
def test_traversal_paths_are_not_relayed(static_port, path):
    with vnc_client(static_port) as client:
        response = client.get(path, auth=BASIC_AUTH)

    assert response.status_code == 404


def test_missing_viewer_file_maps_to_not_found(static_port):
    with vnc_client(static_port) as client:
        response = client.get("/vnc/app/missing.js", auth=BASIC_AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_novnc_server_error_maps_to_bad_gateway(static_port):
    with vnc_client(static_port) as client:
        response = client.get("/vnc/app/broken", auth=BASIC_AUTH)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "VNC_UNAVAILABLE"


def test_unreachable_novnc_server_maps_to_bad_gateway():
    with vnc_client(free_port()) as client:
        response = client.get(VIEWER_PATH, auth=BASIC_AUTH)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "VNC_UNAVAILABLE"


def test_viewer_files_accept_only_get(static_port):
    with vnc_client(static_port) as client:
        response = client.post(VIEWER_PATH, auth=BASIC_AUTH)

    assert response.status_code == 405


@pytest.mark.parametrize(
    "path, allowed",
    [
        ("vnc.html", True),
        ("app/images/icons/novnc-16x16.png", True),
        ("../secret", False),
        ("app/./ui.js", False),
        ("/etc/passwd", False),
        ("app//ui.js", False),
        ("%2e%2e/secret", False),
        ("http://evil.example/x", False),
        ("", False),
    ],
)
def test_asset_path_allow_list(path, allowed):
    assert is_allowed_asset_path(path) is allowed


def test_websocket_relays_frames_with_the_session_cookie(websocket_port):
    with vnc_client(websocket_port) as client:
        client.get("/vnc", auth=BASIC_AUTH)
        with client.websocket_connect(
            WEBSOCKET_PATH, headers=SAME_ORIGIN, subprotocols=["binary"]
        ) as websocket:
            websocket.send_bytes(FRAME)
            echoed = websocket.receive_bytes()
            subprotocol = websocket.accepted_subprotocol

    assert echoed == FRAME
    assert subprotocol == "binary"


def test_websocket_accepts_basic_credentials(websocket_port):
    with vnc_client(websocket_port) as client:
        headers = {**SAME_ORIGIN, "Authorization": basic_header(*BASIC_AUTH)}
        with client.websocket_connect(WEBSOCKET_PATH, headers=headers) as websocket:
            websocket.send_bytes(FRAME)
            echoed = websocket.receive_bytes()

    assert echoed == FRAME


def test_websocket_with_a_wrong_password_is_refused(websocket_port):
    headers = {**SAME_ORIGIN, "Authorization": basic_header("any-user", "wrong-key")}
    with vnc_client(websocket_port) as client, pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(WEBSOCKET_PATH, headers=headers):
            pass

    assert refused.value.code == POLICY_VIOLATION


def test_websocket_without_credentials_is_refused(websocket_port):
    with vnc_client(websocket_port) as client, pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(WEBSOCKET_PATH, headers=SAME_ORIGIN):
            pass

    assert refused.value.code == POLICY_VIOLATION


def test_websocket_from_a_foreign_origin_is_refused(websocket_port):
    with vnc_client(websocket_port) as client:
        client.get("/vnc", auth=BASIC_AUTH)
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(WEBSOCKET_PATH, headers=FOREIGN_ORIGIN):
                pass

    assert refused.value.code == POLICY_VIOLATION


def test_websocket_is_refused_when_novnc_is_down():
    with vnc_client(free_port()) as client:
        client.get("/vnc", auth=BASIC_AUTH)
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(WEBSOCKET_PATH, headers=SAME_ORIGIN):
                pass

    assert refused.value.code == INTERNAL_ERROR
