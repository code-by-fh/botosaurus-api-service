"""Runs the real curl_cffi fetcher against a local HTTP server."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.errors import NavigationError, TargetNotAllowedError
from app.fetch.http_fetcher import HttpFetcher, HttpFetchRequest, accept_language_for
from app.url_guard import UrlGuard
from tests.fakes import SERVER_RENDERED_HTML

MAX_BYTES = 4096
LOOPBACK = "127.0.0.1"
ALIAS_HOST = "localhost"
REDIRECT_PREFIX = "/redirect-to/"


class Handler(BaseHTTPRequestHandler):
    routes = {
        "/page": (200, "text/html; charset=utf-8", SERVER_RENDERED_HTML.encode()),
        "/json": (200, "application/json", b"{}"),
        "/huge": (200, "text/html", b"x" * (MAX_BYTES * 2)),
    }

    def do_GET(self):
        if self.path.startswith(REDIRECT_PREFIX):
            self._redirect(self.path.removeprefix(REDIRECT_PREFIX))
            return
        if self.path == "/language":
            self._respond(200, "text/html", self.headers.get("Accept-Language", "").encode())
            return
        self._respond(*self.routes.get(self.path, (404, "text/plain", b"")))

    def _respond(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, target: str):
        self.send_response(302)
        self.send_header("Location", target)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server_port():
    server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


async def alias_is_public(host: str) -> list[str]:
    """``localhost`` counts as public in these tests; the literal loopback IP does not."""
    return ["93.184.215.14"] if host == ALIAS_HOST else [host]


@pytest.fixture
async def fetcher():
    instance = HttpFetcher(UrlGuard(False, alias_is_public), "de-DE", MAX_BYTES * 4)
    yield instance
    await instance.close()


def request_for(port: int, path: str) -> HttpFetchRequest:
    return HttpFetchRequest(f"http://{ALIAS_HOST}:{port}{path}", timeout_seconds=5, proxy_url=None)


def test_accept_language_matches_chrome_format():
    assert accept_language_for("de-DE") == "de-DE,de;q=0.9"
    assert accept_language_for("de") == "de"


@pytest.mark.anyio
async def test_fetches_html_document(fetcher, server_port):
    page = await fetcher.fetch(request_for(server_port, "/page"))

    assert page.is_html_document is True
    assert page.html == SERVER_RENDERED_HTML


@pytest.mark.anyio
async def test_sends_accept_language_of_the_browser_locale(fetcher, server_port):
    page = await fetcher.fetch(request_for(server_port, "/language"))

    assert page.html == "de-DE,de;q=0.9"


@pytest.mark.anyio
async def test_follows_redirects_to_allowed_hosts(fetcher, server_port):
    page = await fetcher.fetch(request_for(server_port, "/redirect-to//page"))

    assert page.final_url == f"http://{ALIAS_HOST}:{server_port}/page"
    assert page.html == SERVER_RENDERED_HTML


@pytest.mark.anyio
async def test_redirect_to_private_address_is_blocked(fetcher, server_port):
    target = f"/redirect-to/http://{LOOPBACK}:{server_port}/page"

    with pytest.raises(TargetNotAllowedError):
        await fetcher.fetch(request_for(server_port, target))


@pytest.mark.anyio
async def test_non_html_response_is_not_a_document(fetcher, server_port):
    page = await fetcher.fetch(request_for(server_port, "/json"))

    assert page.is_html_document is False


@pytest.mark.anyio
async def test_oversized_body_is_truncated_and_not_a_document(server_port):
    small = HttpFetcher(UrlGuard(False, alias_is_public), "de-DE", MAX_BYTES)

    page = await small.fetch(request_for(server_port, "/huge"))
    await small.close()

    assert page.truncated is True
    assert page.is_html_document is False


@pytest.mark.anyio
async def test_connection_failure_is_a_navigation_error(fetcher):
    unused_port = 1

    with pytest.raises(NavigationError, match="HTTP fetch failed"):
        await fetcher.fetch(request_for(unused_port, "/page"))
