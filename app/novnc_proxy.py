"""Reverse-proxy for the noVNC server running on localhost.

When ``NOVNC_PREFIX`` is set (e.g. ``/novnc``), the noVNC static files and
the websockify WebSocket endpoint are served through the existing API port so
that clients need no direct access to the noVNC port (default 6080).

Authentication for the static files is intentionally omitted: the entry point
(``GET /vnc``) is already guarded by HTTP Basic auth, and the VNC password
itself is the second factor for the actual desktop session.
"""

import asyncio
import logging
import os
import urllib.request
from urllib.error import URLError

import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response

log = logging.getLogger("render.vnc_proxy")

_DEFAULT_VNC_PORT = "6080"


def _novnc_base() -> tuple[str, str]:
    """Return ``(http_base, ws_base)`` for the local noVNC server."""
    port = os.environ.get("VNC_PORT", _DEFAULT_VNC_PORT)
    return f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}"


def novnc_proxy_router(prefix: str, vnc_enabled: bool) -> APIRouter:
    """Return an ``APIRouter`` that proxies noVNC at ``prefix``.

    :param prefix: URL prefix, e.g. ``"/novnc"``; must start with ``/``.
    :param vnc_enabled: when ``False`` every route returns 404.
    """
    router = APIRouter()
    clean = "/" + prefix.strip("/")
    # FastAPI route paths must not end with a slash.
    static_path = clean + "/{path:path}"
    ws_path = clean + "/websockify"

    @router.get(static_path)
    async def novnc_static(path: str, request: Request) -> Response:
        """Proxy noVNC static files (HTML, JS, CSS, …) from the local server."""
        if not vnc_enabled:
            raise HTTPException(status_code=404, detail="VNC is disabled")
        http_base, _ = _novnc_base()
        upstream_url = f"{http_base}/{path}"
        query = str(request.query_params)
        if query:
            upstream_url += f"?{query}"
        try:
            resp = await asyncio.to_thread(urllib.request.urlopen, upstream_url, timeout=10)
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            return Response(content=resp.read(), media_type=content_type)
        except URLError as exc:
            log.warning("noVNC proxy HTTP error for /%s: %s", path, exc)
            raise HTTPException(status_code=502, detail="noVNC server unreachable") from exc

    @router.websocket(ws_path)
    async def novnc_websockify(ws: WebSocket) -> None:
        """Proxy the noVNC websockify WebSocket to the local VNC server."""
        if not vnc_enabled:
            await ws.close(code=4004)
            return
        _, ws_base = _novnc_base()
        # Forward the subprotocol(s) the browser negotiated (typically "binary").
        raw = ws.headers.get("sec-websocket-protocol", "binary")
        subprotocols = [s.strip() for s in raw.split(",") if s.strip()]
        chosen = subprotocols[:1] or ["binary"]
        await ws.accept(subprotocol=chosen[0])
        log.debug("noVNC WebSocket proxy opened → %s/websockify", ws_base)
        try:
            async with websockets.connect(
                f"{ws_base}/websockify",
                subprotocols=chosen,
            ) as upstream:
                await _relay(ws, upstream)
        except Exception as exc:
            log.info("noVNC WebSocket proxy closed: %s", exc)

    return router


async def _relay_from_client(
    ws: WebSocket, upstream: websockets.WebSocketClientProtocol
) -> None:
    """Relay messages from browser client to upstream noVNC server."""
    try:
        while True:
            msg = await ws.receive()
            raw_bytes = msg.get("bytes")
            raw_text = msg.get("text")
            if raw_bytes is not None:
                await upstream.send(raw_bytes)
            elif raw_text is not None:
                await upstream.send(raw_text)
    except Exception:
        pass


async def _relay_from_upstream(
    ws: WebSocket, upstream: websockets.WebSocketClientProtocol
) -> None:
    """Relay messages from upstream noVNC server to browser client."""
    try:
        async for msg in upstream:
            if isinstance(msg, bytes):
                await ws.send_bytes(msg)
            else:
                await ws.send_text(msg)
    except Exception:
        pass


async def _relay(ws: WebSocket, upstream: websockets.WebSocketClientProtocol) -> None:
    """Relay messages in both directions until one side disconnects."""
    tasks = {
        asyncio.create_task(_relay_from_client(ws, upstream)),
        asyncio.create_task(_relay_from_upstream(ws, upstream)),
    }
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
