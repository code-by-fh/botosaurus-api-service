"""Security headers added to every HTTP response.

Only framing is restricted by the Content-Security-Policy: the ``/vnc`` page
frames ``/vnc/app/...`` from the same origin, and Swagger UI loads its assets
from a CDN, so a restrictive ``default-src`` would break the documentation.
"""

from fastapi import FastAPI, Request

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": "frame-ancestors 'self'",
}


def install_security_headers(app: FastAPI) -> None:
    """Register a middleware that sets ``SECURITY_HEADERS`` unless a route set them itself."""

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
