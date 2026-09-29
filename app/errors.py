"""Service error types and the uniform error envelope.

Every error response has the shape
``{"error": {"code": "UPPER_SNAKE", "message": "...", "traceId": "..."}}``;
validation errors additionally carry a per-field ``fields`` list.
"""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("botosaurus.errors")

TRACE_HEADER = "X-Request-ID"
MAX_TRACE_ID_LENGTH = 64
STATUS_CODES_BY_HTTP_STATUS = {
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


class ServiceError(Exception):
    """Base class for errors that map to a well-defined HTTP response."""

    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TargetNotAllowedError(ServiceError):
    """The requested URL points to a destination the service must not contact."""

    status_code = 400
    code = "TARGET_NOT_ALLOWED"


class ProxyNotConfiguredError(ServiceError):
    """``use_proxy`` was requested but no proxy is configured."""

    status_code = 400
    code = "PROXY_NOT_CONFIGURED"


class ElementNotFoundError(ServiceError):
    """The requested CSS selector matched nothing in the rendered page."""

    status_code = 404
    code = "ELEMENT_NOT_FOUND"


class ServiceBusyError(ServiceError):
    """All capacity is in use and the wait queue is full or timed out."""

    status_code = 429
    code = "SERVICE_BUSY"
    retry_after_seconds = 5


class NavigationError(ServiceError):
    """The target could not be loaded (DNS, TLS, connection, HTTP error page)."""

    status_code = 502
    code = "NAVIGATION_FAILED"


class TargetBlockedError(ServiceError):
    """The target answered with an anti-bot challenge that did not resolve."""

    status_code = 502
    code = "TARGET_BLOCKED"


class RenderTimeoutError(ServiceError):
    """The page, or the element in ``wait_for``, did not appear within the timeout."""

    status_code = 504
    code = "TIMEOUT"


def trace_id_of(request: Request) -> str:
    """Return the trace id assigned to ``request`` by the trace middleware."""
    return getattr(request.state, "trace_id", "")


def _envelope(code: str, message: str, trace_id: str) -> dict:
    return {"error": {"code": code, "message": message, "traceId": trace_id}}


async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    headers = {}
    if isinstance(exc, ServiceBusyError):
        headers["Retry-After"] = str(exc.retry_after_seconds)
    body = _envelope(exc.code, exc.message, trace_id_of(request))
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


def _field_name(location: tuple) -> str:
    parts = [str(part) for part in location if part != "body"]
    return ".".join(parts) or "body"


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [
        {"field": _field_name(tuple(error["loc"])), "message": error["msg"]}
        for error in exc.errors()
    ]
    body = _envelope("VALIDATION_ERROR", "Request validation failed", trace_id_of(request))
    body["error"]["fields"] = fields
    return JSONResponse(status_code=400, content=body)


async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = STATUS_CODES_BY_HTTP_STATUS.get(exc.status_code, "HTTP_ERROR")
    message = exc.detail if isinstance(exc.detail, str) else code
    body = _envelope(code, message, trace_id_of(request))
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = trace_id_of(request)
    log.error("Unexpected error (traceId=%s)", trace_id, exc_info=exc)
    body = _envelope("INTERNAL_ERROR", "An unexpected error occurred", trace_id)
    return JSONResponse(status_code=500, content=body)


def _incoming_trace_id(request: Request) -> str:
    candidate = request.headers.get(TRACE_HEADER, "")
    if candidate and len(candidate) <= MAX_TRACE_ID_LENGTH and candidate.isascii():
        return candidate
    return uuid.uuid4().hex


def install_error_handling(app: FastAPI) -> None:
    """Register the trace-id middleware and all exception handlers on ``app``."""

    @app.middleware("http")
    async def assign_trace_id(request: Request, call_next):
        request.state.trace_id = _incoming_trace_id(request)
        response = await call_next(request)
        response.headers[TRACE_HEADER] = request.state.trace_id
        return response

    app.add_exception_handler(ServiceError, _service_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _unexpected_error_handler)
