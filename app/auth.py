"""API-Key authentication for the render service.

The API key is read from the ``API_KEY`` environment variable at startup.

Two auth schemes are provided:
- **Bearer** (``Authorization: Bearer <key>``) for programmatic API access.
- **HTTP Basic** (username ignored, password = API key) for browser-facing
  pages like ``/vnc`` — the browser shows a native login dialog.

If ``API_KEY`` is not set, the service refuses to start — there is no
unauthenticated fallback, which prevents accidental exposure.
"""

import hmac
import os
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

_bearer_scheme = HTTPBearer(
    scheme_name="Bearer API-Key",
    description="Pass the API key as a Bearer token: `Authorization: Bearer <key>`",
)

_basic_scheme = HTTPBasic(
    scheme_name="Basic Auth",
    realm="botosaurus-vnc",
)

_api_key: str | None = None


def _get_api_key() -> str:
    """Return the configured API key (cached after first call)."""
    global _api_key
    if _api_key is None:
        raw = os.environ.get("API_KEY", "").strip()
        if not raw:
            raise RuntimeError(
                "API_KEY environment variable is not set. "
                "The service cannot start without an API key."
            )
        _api_key = raw
    return _api_key


def verify_api_key(
    credentials: Annotated[
        HTTPAuthorizationCredentials, Security(_bearer_scheme)
    ],
) -> None:
    """FastAPI dependency – rejects requests with an invalid or missing key.

    Uses ``hmac.compare_digest`` for constant-time comparison to prevent
    timing side-channel attacks.
    """
    expected = _get_api_key()
    provided = credentials.credentials
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "detail": "Invalid API key"},
        )


def verify_basic_auth(
    credentials: Annotated[HTTPBasicCredentials, Depends(_basic_scheme)],
) -> None:
    """FastAPI dependency for browser-facing pages (e.g. /vnc).

    The browser shows a native login dialog. The username is ignored;
    the password is validated against the API key using constant-time
    comparison.
    """
    expected = _get_api_key()
    # constant-time compare to prevent timing attacks
    password_ok = hmac.compare_digest(
        credentials.password.encode(), expected.encode()
    )
    if not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="botosaurus-vnc"'},
        )


def reset_cached_key() -> None:
    """Reset the cached API key (for testing only)."""
    global _api_key
    _api_key = None

