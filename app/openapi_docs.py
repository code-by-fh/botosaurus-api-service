"""OpenAPI descriptions of response headers, error bodies and the trace header.

The render endpoints return raw HTML or Markdown plus diagnostic headers, and
all errors share one envelope. FastAPI cannot infer either from the code, so
they are declared here and attached to the routes in ``app.main``.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field

from app.auth_throttle import AUTH_FAILURE_WINDOW_SECONDS, MAX_AUTH_FAILURES
from app.errors import TRACE_HEADER, ServiceBusyError
from app.security_headers import SECURITY_HEADERS
from app.vnc_session import SESSION_COOKIE_NAME


class FieldError(BaseModel):
    """One invalid request field."""

    field: str = Field(description="Dotted path of the field, e.g. `timeout`")
    message: str = Field(description="Why the value was rejected")


class ErrorDetail(BaseModel):
    """Error details."""

    code: str = Field(description="Machine-readable UPPER_SNAKE_CASE code")
    message: str = Field(description="Human-readable explanation")
    traceId: str = Field(description=f"Same value as the `{TRACE_HEADER}` response header")
    fields: list[FieldError] | None = Field(
        default=None, description="Only for `VALIDATION_ERROR`: one entry per invalid field"
    )


class ErrorResponse(BaseModel):
    """Envelope of every error response."""

    error: ErrorDetail


def _header(description: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"description": description, "schema": schema or {"type": "string"}}


TRACE_RESPONSE_HEADER = _header(
    "Trace id of this request: the caller's `X-Request-ID` if one was sent, otherwise generated."
)

SECURITY_RESPONSE_HEADERS = {
    name: _header(f"Always `{value}`; set on every response.")
    for name, value in SECURITY_HEADERS.items()
}

RENDER_SUCCESS_HEADERS = {
    "X-Render-Engine": _header(
        "Engine that produced the content: `http` (verified fast path) or `browser` (Chrome).",
        {"type": "string", "enum": ["http", "browser"]},
    ),
    "X-Render-Stable": _header(
        "`false` if the content was still changing when it was returned (timeout reached, or "
        "`wait_for` present for `wait_for_settle` seconds on a page that never settled).",
        {"type": "string", "enum": ["true", "false"]},
    ),
    "X-Final-Url": _header("URL after all redirects, percent-encoded."),
    "X-Upstream-Status": _header(
        "HTTP status the target returned for the main document (`0` if unknown). Error pages "
        "such as 404 are still returned as content with status 200.",
        {"type": "integer"},
    ),
    TRACE_HEADER: TRACE_RESPONSE_HEADER,
    **SECURITY_RESPONSE_HEADERS,
}


def _error(description: str, extra_headers: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "headers": {
            TRACE_HEADER: TRACE_RESPONSE_HEADER,
            **SECURITY_RESPONSE_HEADERS,
            **(extra_headers or {}),
        },
    }


def _retry_after(description: str) -> dict[str, Any]:
    return {"Retry-After": _header(description, {"type": "integer"})}


UNAUTHORIZED_RESPONSE = _error(
    "`UNAUTHORIZED`: missing or unknown API key.",
    {"WWW-Authenticate": _header("Authentication scheme to use.")},
)

AUTH_THROTTLED_DESCRIPTION = (
    f"`TOO_MANY_AUTH_FAILURES`: {MAX_AUTH_FAILURES} wrong API keys from this client within "
    f"{AUTH_FAILURE_WINDOW_SECONDS // 60} minutes; every request is refused until `Retry-After`."
)

AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: UNAUTHORIZED_RESPONSE,
    429: _error(AUTH_THROTTLED_DESCRIPTION, _retry_after("Seconds until the lockout ends.")),
}

RENDER_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: _error(
        "`VALIDATION_ERROR` (with `fields`), `TARGET_NOT_ALLOWED` (non-http(s) URL or a host "
        "resolving to a private or reserved address) or `PROXY_NOT_CONFIGURED`."
    ),
    401: UNAUTHORIZED_RESPONSE,
    404: _error("`ELEMENT_NOT_FOUND`: `selector` matched nothing in the rendered page."),
    429: _error(
        "`SERVICE_BUSY`: wait queue full or no browser/host slot became free in time. "
        f"Or {AUTH_THROTTLED_DESCRIPTION}",
        _retry_after(
            "Seconds to wait before retrying (`SERVICE_BUSY`: currently "
            f"{ServiceBusyError.retry_after_seconds}; `TOO_MANY_AUTH_FAILURES`: until the lockout "
            "ends)."
        ),
    ),
    500: _error("`INTERNAL_ERROR`: unexpected failure; the `traceId` is in the service log."),
    502: _error(
        "`NAVIGATION_FAILED` (DNS, connection or TLS failure, or a redirect to a forbidden "
        "address) or `TARGET_BLOCKED` (an anti-bot challenge did not resolve)."
    ),
    504: _error(
        "`TIMEOUT`: `wait_for` never appeared (at `timeout`, or earlier with `idle_timeout`), "
        "or the browser stopped responding."
    ),
}


def render_responses() -> dict:
    """Responses of the render route."""
    success = {
        "description": "The rendered page, or the element selected by `selector`.",
        "headers": RENDER_SUCCESS_HEADERS,
        "content": {
            "text/html": {"schema": {"type": "string"}},
            "text/markdown": {"schema": {"type": "string"}},
        },
    }
    return {200: success, **RENDER_ERROR_RESPONSES}


def vnc_page_responses() -> dict:
    """Responses of ``GET /vnc``."""
    success = {
        "description": (
            "Viewer page framing `/vnc/app/vnc.html`. Sets the HttpOnly session cookie "
            f"`{SESSION_COOKIE_NAME}` (Path=/vnc, SameSite=Strict) used by the viewer's files "
            "and WebSocket."
        ),
        "headers": {
            "Set-Cookie": _header("The viewer session cookie."),
            **SECURITY_RESPONSE_HEADERS,
        },
        "content": {"text/html": {"schema": {"type": "string"}}},
    }
    return {200: success, **AUTH_RESPONSES}


def vnc_asset_responses() -> dict:
    """Responses of ``GET /vnc/app/{asset_path}``."""
    success = {
        "description": "The noVNC file, relayed unchanged.",
        "headers": SECURITY_RESPONSE_HEADERS,
        "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
    }
    return {
        200: success,
        **AUTH_RESPONSES,
        404: _error("`NOT_FOUND`: path not allowed or not part of noVNC."),
        502: _error("`VNC_UNAVAILABLE`: the in-container noVNC server did not answer."),
    }


TRACE_REQUEST_PARAMETER = {
    "parameters": [
        {
            "name": TRACE_HEADER,
            "in": "header",
            "required": False,
            "description": (
                "Optional trace id (at most 64 ASCII characters) echoed in the response and "
                "the logs, to correlate a call with the service log."
            ),
            "schema": {"type": "string", "maxLength": 64},
        }
    ]
}


VNC_PAGE_PATH = "/vnc"
BEARER_PATHS = frozenset({"/api/v1/render", "/health/detail"})


def install_openapi(app: FastAPI) -> None:
    """Generate the schema without FastAPI's default 422 responses.

    The service answers validation errors with 400 in its own envelope (see
    ``app.errors``), so the generated 422 entries would be wrong.
    """

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title, version=app.version, routes=app.routes, description=app.description
            )
            _drop_default_validation_responses(schema)
            _configure_security_schemes(schema)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi


def _drop_default_validation_responses(schema: dict[str, Any]) -> None:
    for operations in schema.get("paths", {}).values():
        for operation in operations.values():
            operation.get("responses", {}).pop("422", None)
    components = schema.get("components", {}).get("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        components.pop(name, None)


SECURITY_SCHEMES = {
    "HTTPBearer": {
        "type": "http",
        "scheme": "bearer",
        "description": "API Key passed as Bearer token in the Authorization header.",
    },
    "HTTPBasic": {
        "type": "http",
        "scheme": "basic",
        "description": "HTTP Basic for the VNC viewer (any username, password = API key).",
    },
    "VncSession": {
        "type": "apiKey",
        "in": "cookie",
        "name": SESSION_COOKIE_NAME,
        "description": "Session cookie set by `GET /vnc`; accepted by the viewer's files.",
    },
}


def _configure_security_schemes(schema: dict[str, Any]) -> None:
    schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
    for path, methods in schema.get("paths", {}).items():
        for operation in methods.values():
            operation["security"] = _security_of(path)


def _security_of(path: str) -> list[dict[str, list]]:
    if path == VNC_PAGE_PATH:
        return [{"HTTPBasic": []}]
    if path.startswith(VNC_PAGE_PATH):
        return [{"HTTPBasic": []}, {"VncSession": []}]
    return [{"HTTPBearer": []}] if path in BEARER_PATHS else []
