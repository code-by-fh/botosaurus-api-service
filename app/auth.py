"""API-key authentication.

Several keys can be configured (``API_KEYS``, comma-separated) so that every
client application gets its own key and can be revoked independently.

Two schemes are offered:
- **Bearer** (``Authorization: Bearer <key>``) for programmatic API access.
- **HTTP Basic** (username ignored, password = API key) for the ``/vnc`` page,
  so that browsers show their native login dialog.
"""

import hmac
from collections.abc import Callable, Iterable
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

BASIC_REALM = "botosaurus-vnc"

_bearer_scheme = HTTPBearer(
    scheme_name="Bearer API-Key",
    description="Pass the API key as a Bearer token: `Authorization: Bearer <key>`",
    auto_error=False,
)
_basic_scheme = HTTPBasic(scheme_name="Basic Auth", realm=BASIC_REALM, auto_error=False)


def _matches_any(candidate: str, keys: Iterable[str]) -> bool:
    """Compare against every key in constant time, without short-circuiting."""
    encoded = candidate.encode()
    results = [hmac.compare_digest(encoded, key.encode()) for key in keys]
    return any(results)


def bearer_guard(keys: tuple[str, ...]) -> Callable[..., None]:
    """Build a FastAPI dependency that requires one of ``keys`` as Bearer token.

    :raises HTTPException: 401 when the header is missing or the key is unknown.
    """

    def verify(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)],
    ) -> None:
        if credentials is None or not _matches_any(credentials.credentials, keys):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return verify


def basic_guard(keys: tuple[str, ...]) -> Callable[..., None]:
    """Build a FastAPI dependency that requires one of ``keys`` as Basic-auth password.

    :raises HTTPException: 401 with a ``WWW-Authenticate`` challenge on failure.
    """

    def verify(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)],
    ) -> None:
        if credentials is None or not _matches_any(credentials.password, keys):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": f'Basic realm="{BASIC_REALM}"'},
            )

    return verify
