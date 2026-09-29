"""Runs the egress proxies against a local HTTP server."""

import asyncio
import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.egress import EgressGateway, parse_upstream
from app.url_guard import UrlGuard

LOOPBACK = "127.0.0.1"
UNREACHABLE_LOOPBACK = "127.0.0.2"
PAGE_BODY = b"<html><body>egress ok</body></html>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE_BODY)))
        self.send_header("X-Seen-Path", self.path)
        self.end_headers()
        self.wfile.write(PAGE_BODY)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def target_port():
    server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


async def private_resolver(host: str) -> list[str]:
    return ["10.0.0.1"]


async def started_gateway(guard: UrlGuard, home_proxy: str | None = None) -> EgressGateway:
    gateway = EgressGateway(guard, home_proxy)
    await gateway.start()
    return gateway


async def send_through(proxy_url: str, request: bytes) -> bytes:
    host, port = proxy_url.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(request)
    await writer.drain()
    response = await reader.read()
    writer.close()
    return response


def plain_get(port: int) -> bytes:
    return f"GET http://{LOOPBACK}:{port}/page?q=1 HTTP/1.1\r\nHost: x\r\n\r\n".encode()


@pytest.mark.anyio
async def test_plain_http_is_forwarded_in_origin_form(target_port):
    gateway = await started_gateway(UrlGuard(allow_private=True))

    response = await send_through(gateway.url_for(False), plain_get(target_port))
    await gateway.close()

    assert response.startswith(b"HTTP/1.0 200")
    assert b"X-Seen-Path: /page?q=1" in response
    assert response.endswith(PAGE_BODY)


@pytest.mark.anyio
async def test_connect_opens_a_tunnel(target_port):
    gateway = await started_gateway(UrlGuard(allow_private=True))
    request = (
        f"CONNECT {LOOPBACK}:{target_port} HTTP/1.1\r\n\r\n"
        "GET /tunnel HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    ).encode()

    response = await send_through(gateway.url_for(False), request)
    await gateway.close()

    assert response.startswith(b"HTTP/1.1 200 Connection Established")
    assert b"X-Seen-Path: /tunnel" in response


@pytest.mark.anyio
async def test_private_destinations_are_refused(target_port):
    gateway = await started_gateway(UrlGuard(allow_private=False, resolver=private_resolver))
    request = f"CONNECT metadata.internal:{target_port} HTTP/1.1\r\n\r\n".encode()

    response = await send_through(gateway.url_for(False), request)
    await gateway.close()

    assert response.startswith(b"HTTP/1.1 403")


@pytest.mark.anyio
async def test_proxied_route_is_chained_through_the_upstream(target_port):
    upstream = await started_gateway(UrlGuard(allow_private=True))
    gateway = await started_gateway(UrlGuard(allow_private=True), upstream.url_for(False))

    response = await send_through(gateway.url_for(True), plain_get(target_port))
    await gateway.close()
    await upstream.close()

    assert response.startswith(b"HTTP/1.0 200")
    assert response.endswith(PAGE_BODY)


@pytest.mark.anyio
async def test_proxied_route_is_unavailable_without_home_proxy():
    gateway = EgressGateway(UrlGuard(allow_private=True), None)

    with pytest.raises(ValueError, match="no proxied egress"):
        gateway.url_for(True)


def test_upstream_credentials_are_decoded():
    upstream = parse_upstream("http://user%40home:p%3Ass@proxy.example:3128")

    assert (upstream.username, upstream.password) == ("user@home", "p:ss")
    assert (upstream.host, upstream.port) == ("proxy.example", 3128)


def test_unsupported_upstream_scheme_is_rejected():
    with pytest.raises(ValueError, match="HOME_PROXY"):
        parse_upstream("ftp://proxy.example")


class KeepAliveServer:
    """Answers with keep-alive and never closes; counts the requests it receives."""

    def __init__(self):
        self.requests = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, LOOPBACK, 0)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            await self._answer_forever(reader, writer)

    async def _answer_forever(self, reader, writer):
        while await reader.readuntil(b"\r\n\r\n"):
            self.requests += 1
            writer.write(
                b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nContent-Length: 2\r\n\r\nok"
            )
            await writer.drain()

    async def close(self):
        self._server.close()


@pytest.mark.anyio
async def test_plain_http_connection_carries_exactly_one_request():
    upstream = KeepAliveServer()
    port = await upstream.start()
    gateway = await started_gateway(UrlGuard(allow_private=True))
    first = f"GET http://{LOOPBACK}:{port}/one HTTP/1.1\r\nHost: a\r\n\r\n"
    second = "GET http://other.example/two HTTP/1.1\r\nHost: other.example\r\n\r\n"
    host, proxy_port = gateway.url_for(False).removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(proxy_port))

    writer.write((first + second).encode())
    head = await reader.readuntil(b"\r\n\r\n")
    writer.close()
    await gateway.close()
    await upstream.close()

    assert b"Connection: close" in head
    assert b"keep-alive" not in head
    assert upstream.requests == 1


@pytest.mark.anyio
async def test_chunked_request_bodies_are_rejected(target_port):
    gateway = await started_gateway(UrlGuard(allow_private=True))
    request = (
        f"POST http://{LOOPBACK}:{target_port}/form HTTP/1.1\r\n"
        "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    ).encode()

    response = await send_through(gateway.url_for(False), request)
    await gateway.close()

    assert response.startswith(b"HTTP/1.1 411")


@pytest.mark.anyio
async def test_next_vetted_address_is_tried_when_first_is_unreachable(target_port):
    async def two_addresses(host: str) -> list[str]:
        return [UNREACHABLE_LOOPBACK, LOOPBACK]

    class OnlyVetting(UrlGuard):
        async def vetted_addresses(self, host: str) -> list[str]:
            return await two_addresses(host)

    gateway = await started_gateway(OnlyVetting(allow_private=False))
    request = (
        f"CONNECT target.example:{target_port} HTTP/1.1\r\n\r\n"
        "GET /fallback HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    ).encode()

    response = await asyncio.wait_for(send_through(gateway.url_for(False), request), 60)
    await gateway.close()

    assert b"X-Seen-Path: /fallback" in response
