"""noVNC live view of the headed browsers, served entirely under ``/vnc``.

``GET /vnc`` (HTTP Basic) returns a page that frames the viewer from the same
origin and sets a short-lived session cookie. The viewer's files
(``/vnc/app/...``) and its WebSocket (``/vnc/websockify``) accept that cookie or
Basic credentials. All paths are relative to the origin, so the view works on
any public domain behind a TLS-terminating proxy.
"""

import html
import logging
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response, WebSocket
from fastapi.responses import HTMLResponse
from starlette import status
from websockets.exceptions import InvalidHandshake

from app.auth import BASIC_CHALLENGE, Authenticator, Guard, basic_guard, basic_password
from app.errors import AuthenticationError, TooManyAuthFailuresError
from app.openapi_docs import vnc_asset_responses, vnc_page_responses
from app.vnc_proxy import VncUpstream, relay
from app.vnc_session import (
    SESSION_COOKIE_NAME,
    SessionSigner,
    is_https,
    is_same_origin,
    set_session_cookie,
)

log = logging.getLogger("render.vnc")

VNC_PATH = "/vnc"
ASSET_PREFIX = f"{VNC_PATH}/app"
WEBSOCKET_PATH = f"{VNC_PATH}/websockify"
# noVNC resolves `path` against the viewer page's URL when no `host` is given
# (app/ui.js: `new URL(path, location.href)`), so a relative value would point
# below /vnc/app/. The absolute path keeps the socket on /vnc/websockify.
VIEWER_QUERY = urlencode(
    {"autoconnect": "true", "resize": "scale", "path": WEBSOCKET_PATH}, safe="/"
)
VIEWER_SRC = f"{ASSET_PREFIX}/vnc.html?{VIEWER_QUERY}"
SUBPROTOCOL_HEADER = "sec-websocket-protocol"


@dataclass(frozen=True)
class VncAccess:
    """What the VNC routes need: key checks, session tokens and the local noVNC server."""

    authenticator: Authenticator
    signer: SessionSigner
    upstream: VncUpstream


def vnc_page() -> str:
    """Return the viewer page, which frames the noVNC client from the same origin."""
    src = html.escape(VIEWER_SRC, quote=True)
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Browser View - noVNC</title>
  <style>
    body, html {{ margin: 0; padding: 0; height: 100%; background: #1a1a1a; }}
    iframe {{ width: 100%; height: 100%; border: none; display: block; }}
  </style>
</head>
<body>
  <iframe src="{src}"></iframe>
</body>
</html>"""


def _is_authorized(access: VncAccess, connection: Request | WebSocket) -> bool:
    if access.signer.is_valid(connection.cookies.get(SESSION_COOKIE_NAME)):
        return True
    return access.authenticator.accepts(connection, basic_password(connection.headers))


def _session_guard(access: VncAccess) -> Guard:
    async def verify(request: Request) -> None:
        if not _is_authorized(access, request):
            raise AuthenticationError("Missing or invalid credentials", BASIC_CHALLENGE)

    return verify


def _may_open_websocket(access: VncAccess, websocket: WebSocket) -> bool:
    if not is_same_origin(websocket):
        origin = websocket.headers.get("origin")
        log.warning("Rejected VNC WebSocket from foreign origin %r", origin)
        return False
    try:
        return _is_authorized(access, websocket)
    except TooManyAuthFailuresError:
        log.warning("Rejected VNC WebSocket from a locked-out client")
        return False


def _offered_subprotocols(websocket: WebSocket) -> list[str]:
    raw = websocket.headers.get(SUBPROTOCOL_HEADER, "")
    return [name.strip() for name in raw.split(",") if name.strip()]


async def _serve_websocket(access: VncAccess, websocket: WebSocket) -> None:
    if not _may_open_websocket(access, websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        upstream = await access.upstream.open_websocket(_offered_subprotocols(websocket))
    except (OSError, TimeoutError, InvalidHandshake) as exc:
        log.warning("noVNC WebSocket unreachable: %s", exc)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return
    async with upstream:
        await websocket.accept(subprotocol=upstream.subprotocol)
        await relay(websocket, upstream)


def _add_viewer_route(router: APIRouter, access: VncAccess) -> None:
    @router.get(
        VNC_PATH,
        summary="noVNC live view of the headed Chrome instances",
        response_class=HTMLResponse,
        dependencies=[Depends(basic_guard(access.authenticator))],
        responses=vnc_page_responses(),
    )
    def viewer(request: Request) -> Response:
        response = HTMLResponse(vnc_page())
        set_session_cookie(response, access.signer.issue(), is_https(request))
        return response


def _add_asset_route(router: APIRouter, access: VncAccess) -> None:
    @router.get(
        f"{ASSET_PREFIX}/{{asset_path:path}}",
        summary="noVNC viewer files, relayed from the in-container noVNC server",
        response_class=Response,
        dependencies=[Depends(_session_guard(access))],
        responses=vnc_asset_responses(),
    )
    async def viewer_asset(asset_path: str, request: Request) -> Response:
        query = urlencode(request.query_params.multi_items())
        asset = await access.upstream.fetch_asset(asset_path, query)
        return Response(content=asset.content, media_type=asset.media_type)


def vnc_router(access: VncAccess) -> APIRouter:
    """Routes of the live view; only mounted when ``ENABLE_VNC=true``."""
    router = APIRouter(tags=["VNC"])
    _add_viewer_route(router, access)
    _add_asset_route(router, access)

    @router.websocket(WEBSOCKET_PATH)
    async def viewer_websocket(websocket: WebSocket) -> None:
        await _serve_websocket(access, websocket)

    return router
