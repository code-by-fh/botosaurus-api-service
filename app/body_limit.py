"""Rejects oversized request bodies before FastAPI buffers and parses them.

FastAPI reads and parses the whole body before the authentication dependency
runs, so without a limit any unauthenticated client could make the service
buffer and parse arbitrarily large bodies. This pure ASGI middleware refuses a
too-large ``Content-Length`` at once and caps bodies without one (chunked
transfer) while reading them, then hands the bounded body on to the application.
"""

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import RequestTooLargeError, error_response, trace_id_of

# The largest valid render request is a 2048-character URL plus a few short fields
# (two 500-character selectors among them); 64 KiB leaves ample headroom.
MAX_REQUEST_BODY_BYTES = 64 * 1024
CONTENT_LENGTH_HEADER = b"content-length"
TOO_LARGE_MESSAGE = f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"


class BodyTooLarge(Exception):
    """The body grew beyond the limit while it was read."""


class BodyLimitMiddleware:
    """Answers ``413 REQUEST_TOO_LARGE`` for HTTP bodies over ``limit`` bytes."""

    def __init__(self, app: ASGIApp, limit: int = MAX_REQUEST_BODY_BYTES):
        self._app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        try:
            if _declared_length(scope) > self._limit:
                raise BodyTooLarge
            body = await _read_body(receive, self._limit)
        except BodyTooLarge:
            await _reject(scope, receive, send)
            return
        # A client that left mid-body gets no answer, as the application would give none.
        if body is not None:
            await self._app(scope, _replay(body, receive), send)


def _declared_length(scope: Scope) -> int:
    # A malformed value is left to the server, which rejects it; it carries no size here.
    for name, value in scope["headers"]:
        if name == CONTENT_LENGTH_HEADER and value.isdigit():
            return int(value)
    return 0


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    """Read the whole body, giving up as soon as it exceeds ``limit``.

    :return: the body, or ``None`` if the client disconnected before sending all of it.
    :raises BodyTooLarge: when more than ``limit`` bytes arrive.
    """
    chunks: list[bytes] = []
    size = 0
    more_body = True
    while more_body:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge
        chunks.append(chunk)
        more_body = message.get("more_body", False)
    return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if delivered:
            # Later calls wait for the disconnect, as the server's receive would.
            return await receive()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
    response = error_response(RequestTooLargeError(TOO_LARGE_MESSAGE), trace_id_of(Request(scope)))
    await response(scope, receive, send)
