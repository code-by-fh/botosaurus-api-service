"""Local forward proxies through which every outgoing request leaves the service.

Chrome and the HTTP fetcher never connect to targets themselves. They talk to
one of these proxies on 127.0.0.1, which checks each destination host with the
``UrlGuard`` before connecting. This covers what a URL check alone cannot:
redirects, meta refreshes, JavaScript navigations, sub-resources and DNS
rebinding. The direct route connects to exactly the address the guard vetted.

Two routes exist: ``direct`` (the VPS's own IP) and, if ``HOME_PROXY`` is set,
``proxied`` (chained to that upstream proxy, credentials included, so Chrome
never needs to authenticate).
"""

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass

from zendriver.core.proxy import (
    DEFAULT_PORTS,
    HOP_BY_HOP_HEADERS,
    TIMEOUT,
    ProxyError,
    UpstreamProxy,
    copy_stream,
    pipe,
    split_authority,
)

from app.errors import ServiceError
from app.url_guard import UrlGuard

log = logging.getLogger("render.egress")

LISTEN_HOST = "127.0.0.1"
HEADER_LIMIT_BYTES = 65536
BLOCKED_STATUS = 403
LENGTH_REQUIRED_STATUS = 411
CLOSE_HEADER = "Connection: close"
RESPONSE_CONNECTION_HEADERS = frozenset({"connection", "keep-alive", "proxy-connection"})

Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]


@dataclass(frozen=True)
class ProxyRequest:
    """Parsed head of a request Chrome or curl sent to the proxy."""

    method: str
    target: str
    version: str
    headers: list[str]

    def header(self, name: str) -> str | None:
        for line in self.headers:
            key, _, value = line.partition(":")
            if key.strip().lower() == name:
                return value.strip()
        return None

    @property
    def body_length(self) -> int:
        """Length of the request body; chunked uploads are not supported.

        :raises ProxyError: 411 for chunked bodies, 400 for a malformed length.
        """
        if self.header("transfer-encoding"):
            raise ProxyError("chunked request bodies are not supported", LENGTH_REQUIRED_STATUS)
        raw = self.header("content-length") or "0"
        if not raw.isdigit():
            raise ProxyError(f"invalid Content-Length: {raw!r}", 400)
        return int(raw)


def _force_close(response_head: bytes) -> bytes:
    """Rewrite a response head so the client closes the connection afterwards."""
    status_line, *lines = response_head.decode("latin-1").split("\r\n")[:-2]
    kept = [
        line
        for line in lines
        if line.split(":", 1)[0].strip().lower() not in RESPONSE_CONNECTION_HEADERS
    ]
    return "\r\n".join([status_line, *kept, CLOSE_HEADER, "", ""]).encode("latin-1")


async def _relay_single_exchange(client: Streams, upstream: Streams, body_length: int) -> None:
    """Relay one plain-HTTP request body and its response, then end both connections.

    A plain-HTTP proxy connection must never carry a second request: that
    request could be meant for another host and would bypass the guard. The
    response is therefore rewritten to ``Connection: close``, and the relay
    stops as soon as either side finishes.
    """
    client_reader, client_writer = client
    upstream_reader, upstream_writer = upstream
    try:
        if body_length:
            upstream_writer.write(await client_reader.readexactly(body_length))
        head = await asyncio.wait_for(upstream_reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        client_writer.write(_force_close(head))
        response = asyncio.create_task(copy_stream(upstream_reader, client_writer))
        client_gone = asyncio.create_task(client_reader.read())
        await asyncio.wait({response, client_gone}, return_when=asyncio.FIRST_COMPLETED)
        for task in (response, client_gone):
            task.cancel()
        await asyncio.gather(response, client_gone, return_exceptions=True)
    finally:
        upstream_writer.close()


def parse_upstream(url: str) -> UpstreamProxy:
    """Parse ``http[s]://`` or ``socks5[h]://`` proxy URLs, with or without credentials.

    :raises ValueError: for an unsupported scheme or a URL without host.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        raise ValueError("HOME_PROXY must be http(s):// or socks5:// with a host")
    return UpstreamProxy(
        scheme=scheme,
        host=parts.hostname,
        port=parts.port or DEFAULT_PORTS[scheme],
        username=urllib.parse.unquote(parts.username or ""),
        password=urllib.parse.unquote(parts.password or ""),
    )


def _origin_form(request: ProxyRequest, url: urllib.parse.SplitResult) -> str:
    path = urllib.parse.urlunsplit(("", "", url.path or "/", url.query, ""))
    return f"{request.method} {path} {request.version}"


async def _connect_first(addresses: list[str], port: int) -> Streams:
    """Connect to the first reachable vetted address (e.g. IPv4 when IPv6 is not routed)."""
    failure: Exception | None = None
    for address in addresses:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), TIMEOUT)
        except (OSError, TimeoutError) as exc:
            failure = exc
    raise ProxyError(f"could not connect to port {port} of {addresses}: {failure!r}")


class GuardedProxy:
    """HTTP forward proxy (CONNECT and plain HTTP) that enforces the ``UrlGuard``."""

    def __init__(self, guard: UrlGuard, upstream: UpstreamProxy | None):
        self._guard = guard
        self._upstream = upstream
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        """``http://127.0.0.1:<port>`` of the running proxy."""
        if self._server is None:
            raise RuntimeError("egress proxy is not started")
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, LISTEN_HOST, 0, limit=HEADER_LIMIT_BYTES
        )

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for task in self._clients:
            task.cancel()
        await asyncio.gather(*self._clients, return_exceptions=True)
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            await self._serve(reader, writer)
        except ProxyError as exc:
            log.info("Egress refused: %s", exc)
            writer.write(
                f"HTTP/1.1 {exc.status} Proxy Error\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n".encode()
            )
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            log.debug("Egress client connection ended", exc_info=True)
        except ValueError:
            log.info("Egress refused a malformed request", exc_info=True)
        finally:
            self._clients.discard(task)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await self._read_request(reader)
        if request.method == "CONNECT":
            host, port = split_authority(request.target)
            upstream_reader, upstream_writer = await self._tunnel(host, port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await pipe(reader, writer, upstream_reader, upstream_writer)
            return
        body_length = request.body_length
        upstream = await self._forward_plain(request)
        await _relay_single_exchange((reader, writer), upstream, body_length)

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> ProxyRequest:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        request_line, *header_lines = head.decode("latin-1").split("\r\n")[:-2]
        try:
            method, target, version = request_line.split(" ")
        except ValueError as exc:
            raise ProxyError(f"malformed request line: {request_line!r}", 400) from exc
        return ProxyRequest(method, target, version, header_lines)

    async def _vet(self, host: str) -> list[str]:
        try:
            return await self._guard.vetted_addresses(host)
        except ServiceError as exc:
            raise ProxyError(f"{host}: {exc.message}", BLOCKED_STATUS) from exc

    async def _tunnel(self, host: str, port: int) -> Streams:
        addresses = await self._vet(host)
        if self._upstream is not None:
            log.debug("CONNECT %s:%d via upstream proxy %s", host, port, self._upstream.host)
            return await self._upstream.open_tunnel(host, port)
        log.debug("CONNECT %s:%d directly to %s", host, port, addresses)
        return await _connect_first(addresses, port)

    async def _forward_plain(self, request: ProxyRequest) -> Streams:
        url = urllib.parse.urlsplit(request.target)
        if url.scheme != "http" or not url.hostname:
            raise ProxyError(f"unsupported request target: {request.target!r}", 400)
        headers = [
            line
            for line in request.headers
            if line.split(":", 1)[0].strip().lower() not in HOP_BY_HOP_HEADERS
        ]
        headers.append(CLOSE_HEADER)
        if self._upstream is not None and not self._upstream.is_socks:
            await self._vet(url.hostname)
            streams = await self._upstream.open_connection()
            request_line = f"{request.method} {request.target} {request.version}"
            if self._upstream.username or self._upstream.password:
                headers.append(f"Proxy-Authorization: {self._upstream.authorization}")
        else:
            streams = await self._tunnel(url.hostname, url.port or 80)
            request_line = _origin_form(request, url)
        streams[1].write("\r\n".join([request_line, *headers, "", ""]).encode("latin-1"))
        return streams


class EgressGateway:
    """Owns the direct and (optional) proxied egress proxies."""

    def __init__(self, guard: UrlGuard, home_proxy_url: str | None):
        self._direct = GuardedProxy(guard, upstream=None)
        self._proxied = (
            GuardedProxy(guard, parse_upstream(home_proxy_url)) if home_proxy_url else None
        )

    @property
    def has_proxy(self) -> bool:
        return self._proxied is not None

    def url_for(self, use_proxy: bool) -> str:
        """Local proxy address for the requested route.

        :raises ValueError: if the proxied route is requested but not configured.
        """
        if not use_proxy:
            url = self._direct.url
            log.debug("Egress route: direct (%s)", url)
            return url
        if self._proxied is None:
            raise ValueError("no proxied egress configured")
        url = self._proxied.url
        log.debug("Egress route: proxied via HOME_PROXY (%s)", url)
        return url

    async def start(self) -> None:
        await self._direct.start()
        if self._proxied is not None:
            await self._proxied.start()
            log.info(
                "Egress gateway started: direct=%s | proxied=%s (HOME_PROXY configured)",
                self._direct.url,
                self._proxied.url,
            )
        else:
            log.info("Egress gateway started: direct=%s (no HOME_PROXY)", self._direct.url)

    async def close(self) -> None:
        await self._direct.close()
        if self._proxied is not None:
            await self._proxied.close()
