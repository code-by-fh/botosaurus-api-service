"""Connection to the in-container noVNC server (websockify on 127.0.0.1).

The viewer's static files and its WebSocket are relayed through the API port,
so websockify never has to be reachable from outside the container.
"""

import asyncio
import logging
import re
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.errors import ResourceNotFoundError, VncUnavailableError

log = logging.getLogger("render.vnc")

LOOPBACK = "127.0.0.1"
WEBSOCKIFY_PATH = "websockify"
UPSTREAM_TIMEOUT_SECONDS = 10
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_MEDIA_TYPE = "application/octet-stream"
HTTP_NOT_FOUND = 404
ALLOWED_SUBPROTOCOLS = ("binary",)
# One or more plain path segments: no percent escapes, no scheme, no leading slash.
ASSET_PATH_PATTERN = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
DOT_SEGMENTS = frozenset({".", ".."})


@dataclass(frozen=True)
class VncAsset:
    """A static file of the noVNC viewer."""

    content: bytes
    media_type: str


def is_allowed_asset_path(path: str) -> bool:
    """Return whether ``path`` is a plain relative file path without traversal.

    Starlette has already percent-decoded the path, so an encoded ``..`` or
    ``/`` arrives here decoded and a double-encoded one still contains ``%``.
    """
    if not ASSET_PATH_PATTERN.fullmatch(path):
        return False
    return not any(segment in DOT_SEGMENTS for segment in path.split("/"))


class VncUpstream:
    """Fetches viewer files from and opens WebSockets to the local websockify."""

    def __init__(self, port: int):
        self._http_base = f"http://{LOOPBACK}:{port}"
        self._websocket_url = f"ws://{LOOPBACK}:{port}/{WEBSOCKIFY_PATH}"
        # An empty ProxyHandler keeps HTTP(S)_PROXY variables from diverting loopback calls.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    async def fetch_asset(self, path: str, query: str) -> VncAsset:
        """Return the viewer file at ``path``; ``query`` must already be URL-encoded.

        :raises ResourceNotFoundError: for a disallowed path or a file noVNC does not have.
        :raises VncUnavailableError: when websockify does not answer properly.
        """
        if not is_allowed_asset_path(path):
            raise ResourceNotFoundError("Unknown VNC viewer file")
        url = f"{self._http_base}/{path}" + (f"?{query}" if query else "")
        return await asyncio.to_thread(self._read, url)

    def _read(self, url: str) -> VncAsset:
        try:
            with self._opener.open(url, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
                content = response.read(MAX_ASSET_BYTES + 1)
                media_type = response.headers.get("Content-Type", DEFAULT_MEDIA_TYPE)
        except HTTPError as exc:
            exc.close()
            raise _mapped_http_error(exc) from exc
        except OSError as exc:
            log.warning("noVNC server unreachable: %s", exc)
            raise VncUnavailableError("The VNC viewer is not reachable") from exc
        if len(content) > MAX_ASSET_BYTES:
            raise VncUnavailableError("The VNC viewer file is too large")
        return VncAsset(content, media_type)

    async def open_websocket(self, offered: list[str]) -> ClientConnection:
        """Connect to websockify with the subset of ``offered`` subprotocols it may use.

        :raises OSError, TimeoutError, websockets.InvalidHandshake: when websockify is down.
        """
        return await connect(
            self._websocket_url,
            subprotocols=[name for name in offered if name in ALLOWED_SUBPROTOCOLS] or None,
            proxy=None,
            compression=None,
            max_size=MAX_FRAME_BYTES,
            open_timeout=UPSTREAM_TIMEOUT_SECONDS,
        )


def _mapped_http_error(error: HTTPError) -> Exception:
    if error.code == HTTP_NOT_FOUND:
        return ResourceNotFoundError("Unknown VNC viewer file")
    log.warning("noVNC server answered with status %s", error.code)
    return VncUnavailableError("The VNC viewer answered with an error")


async def _client_to_upstream(client: WebSocket, upstream: ClientConnection) -> None:
    while True:
        message = await client.receive()
        if message["type"] == "websocket.disconnect":
            return
        payload = message.get("bytes")
        if payload is None:
            payload = message.get("text")
        if payload is not None:
            await upstream.send(payload)


async def _upstream_to_client(client: WebSocket, upstream: ClientConnection) -> None:
    async for message in upstream:
        if isinstance(message, bytes):
            await client.send_bytes(message)
        else:
            await client.send_text(message)


async def _until_disconnect(direction: str, pump) -> None:
    # Either side hanging up is the normal end of a viewer session.
    try:
        await pump
    except (ConnectionClosed, WebSocketDisconnect) as exc:
        log.debug("VNC relay %s ended: %r", direction, exc)


async def relay(client: WebSocket, upstream: ClientConnection) -> None:
    """Copy messages in both directions until one side disconnects, then close the client."""
    tasks = [
        asyncio.create_task(_until_disconnect("to noVNC", _client_to_upstream(client, upstream))),
        asyncio.create_task(_until_disconnect("to client", _upstream_to_client(client, upstream))),
    ]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending)
    for task in tasks:
        if not task.cancelled() and task.exception() is not None:
            log.error("VNC relay failed", exc_info=task.exception())
    await _close_quietly(client)


async def _close_quietly(client: WebSocket) -> None:
    open_states = (client.client_state, client.application_state)
    if all(state == WebSocketState.CONNECTED for state in open_states):
        try:
            await client.close()
        except WebSocketDisconnect:
            log.debug("VNC client was gone before the close frame")
