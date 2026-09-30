"""Short-lived signed session cookie for the noVNC viewer.

Browsers do not reliably send Basic credentials with WebSocket upgrades or with
every iframe sub-resource. After a successful Basic login on ``GET /vnc`` the
service therefore sets a cookie that the viewer's own requests carry.

The token is ``<expiry>.<hmac>``; the HMAC key is random per process, so a
restart invalidates every session. That is intended: nothing has to be stored,
and a leaked cookie dies with the process at the latest.
"""

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import Response
from starlette.requests import HTTPConnection

SESSION_COOKIE_NAME = "vnc_session"
SESSION_COOKIE_PATH = "/vnc"
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_SECRET_BYTES = 32
MAX_TOKEN_LENGTH = 128
TOKEN_PURPOSE = "vnc-session"
TOKEN_SEPARATOR = "."
SECURE_SCHEMES = frozenset({"https", "wss"})
FORWARDED_PROTO_HEADER = "x-forwarded-proto"
FORWARDED_HOST_HEADER = "x-forwarded-host"

Clock = Callable[[], float]


class SessionSigner:
    """Issues and verifies expiring, HMAC-signed session tokens."""

    def __init__(self, clock: Clock = time.time):
        self._secret = secrets.token_bytes(SESSION_SECRET_BYTES)
        self._clock = clock

    def issue(self) -> str:
        """Return a new token that is valid for ``SESSION_TTL_SECONDS``."""
        expiry = str(int(self._clock()) + SESSION_TTL_SECONDS)
        return f"{expiry}{TOKEN_SEPARATOR}{self._sign(expiry)}"

    def is_valid(self, token: str | None) -> bool:
        """Return whether ``token`` was issued by this process and has not expired."""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        expiry, _, signature = token.partition(TOKEN_SEPARATOR)
        if not (expiry.isascii() and expiry.isdigit()):
            return False
        authentic = hmac.compare_digest(signature.encode(), self._sign(expiry).encode())
        return authentic and int(expiry) > self._clock()

    def _sign(self, expiry: str) -> str:
        message = f"{TOKEN_PURPOSE}:{expiry}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()


def is_https(connection: HTTPConnection) -> bool:
    """Return whether the client reached the service over TLS.

    TLS terminates at the reverse proxy, so ``X-Forwarded-Proto`` is honoured.
    It only decides the cookie's ``Secure`` flag: a spoofed value can only
    weaken or strengthen the spoofer's own cookie.
    """
    forwarded = connection.headers.get(FORWARDED_PROTO_HEADER, "")
    scheme = forwarded.split(",")[0].strip().lower() or connection.url.scheme
    return scheme in SECURE_SCHEMES


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    """Attach the session cookie, scoped to the viewer's paths only."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        path=SESSION_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def is_same_origin(connection: HTTPConnection) -> bool:
    """Return whether the ``Origin`` header names the host the request was sent to.

    Blocks cross-site WebSocket hijacking: a foreign page can open a WebSocket
    to this host, but its browser always sends that page's own origin.
    ``X-Forwarded-Host`` is accepted as well for proxies that rewrite ``Host``;
    a hijacking page cannot set that header on the victim's upgrade request.
    """
    origin_host = urlsplit(connection.headers.get("origin", "")).netloc.lower()
    if not origin_host:
        return False
    forwarded_host = connection.headers.get(FORWARDED_HOST_HEADER, "").split(",")[0]
    candidates = {connection.headers.get("host", ""), forwarded_host.strip()}
    return origin_host in {candidate.lower() for candidate in candidates if candidate}
