"""API-key authentication with brute-force protection.

Several keys can be configured (``API_KEYS``, comma-separated) so that every
client application gets its own key and can be revoked independently.

Two schemes are offered:
- **Bearer** (``Authorization: Bearer <key>``) for programmatic API access.
- **HTTP Basic** (username ignored, password = API key) for the browser pages
  (``/vnc``, ``/docs``), so that browsers show their native login dialog.

Clients that present wrong keys too often get ``429`` instead of ``401`` for
their wrong keys for a while; a valid key always passes.
"""

import base64
import binascii
import hmac
import logging
from collections.abc import Callable, Coroutine, Iterable
from typing import Annotated, Any

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection

from app.auth_throttle import FailedAuthLimiter
from app.client_address import client_address
from app.config import IpNetwork
from app.errors import AuthenticationError, TooManyAuthFailuresError

log = logging.getLogger("render.auth")

BASIC_REALM = "page-render-service"
BASIC_CHALLENGE = f'Basic realm="{BASIC_REALM}", charset="UTF-8"'
BEARER_CHALLENGE = "Bearer"
BASIC_SCHEME = "basic"
AUTHORIZATION_HEADER = "authorization"

Guard = Callable[..., Coroutine[Any, Any, None]]

_bearer_scheme = HTTPBearer(
    scheme_name="Bearer API-Key",
    description="Pass the API key as a Bearer token: `Authorization: Bearer <key>`",
    auto_error=False,
)


def _matches_any(candidate: str, keys: Iterable[str]) -> bool:
    """Compare against every key in constant time, without short-circuiting."""
    encoded = candidate.encode()
    results = [hmac.compare_digest(encoded, key.encode()) for key in keys]
    return any(results)


def basic_password(headers: Headers) -> str | None:
    """Return the password of an ``Authorization: Basic`` header, ``None`` if absent.

    A malformed header is treated like a missing one: it cannot carry a key, so
    the caller answers with the normal challenge.
    """
    scheme, _, encoded = headers.get(AUTHORIZATION_HEADER, "").partition(" ")
    if scheme.lower() != BASIC_SCHEME or not encoded.strip():
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    _, separator, password = decoded.partition(":")
    return password if separator else None


class Authenticator:
    """Verifies API keys and throttles clients that keep presenting wrong ones."""

    def __init__(
        self, keys: tuple[str, ...], limiter: FailedAuthLimiter, trusted: tuple[IpNetwork, ...]
    ):
        self._keys = keys
        self._limiter = limiter
        self._trusted = trusted

    def accepts(self, connection: HTTPConnection, candidate: str | None) -> bool:
        """Return whether ``candidate`` is a configured key; count it if it is wrong.

        A valid key always passes, even from a locked-out address: behind a reverse
        proxy many clients can share one address, and a lockout that also refused
        valid keys would let anyone lock every client out. Missing credentials are
        not counted: they cannot guess a key, and browsers send one unauthenticated
        request before showing the login dialog.

        :raises TooManyAuthFailuresError: for a wrong key while the client is locked out.
        """
        if candidate is None:
            return False
        if _matches_any(candidate, self._keys):
            return True
        client = client_address(connection, self._trusted)
        retry_after = self._limiter.retry_after_seconds(client)
        if retry_after:
            raise TooManyAuthFailuresError(retry_after)
        log.warning("Rejected invalid API key from %s", client)
        self._limiter.record_failure(client)
        return False


def bearer_guard(authenticator: Authenticator) -> Guard:
    """Build a FastAPI dependency that requires one of the keys as Bearer token.

    :raises AuthenticationError: 401 when the header is missing or the key is unknown.
    :raises TooManyAuthFailuresError: 429 for a wrong key while the client is locked out.
    """

    async def verify(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)],
    ) -> None:
        token = credentials.credentials if credentials else None
        if not authenticator.accepts(request, token):
            raise AuthenticationError("Missing or invalid API key", BEARER_CHALLENGE)

    return verify


def basic_guard(authenticator: Authenticator) -> Guard:
    """Build a FastAPI dependency that requires one of the keys as Basic-auth password.

    :raises AuthenticationError: 401 with a ``WWW-Authenticate`` challenge on failure.
    :raises TooManyAuthFailuresError: 429 for a wrong key while the client is locked out.
    """

    async def verify(request: Request) -> None:
        if not authenticator.accepts(request, basic_password(request.headers)):
            raise AuthenticationError("Missing or invalid credentials", BASIC_CHALLENGE)

    return verify
