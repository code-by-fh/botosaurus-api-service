"""Swagger UI, ReDoc and the OpenAPI schema behind HTTP Basic auth.

FastAPI's built-in documentation routes are public and cannot take
dependencies, so they are disabled in ``app.main`` and replaced here. The
routes are only mounted when ``ENABLE_DOCS=true``.
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import Authenticator, basic_guard

OPENAPI_PATH = "/openapi.json"
SWAGGER_PATH = "/docs"
OAUTH2_REDIRECT_PATH = "/docs/oauth2-redirect"
REDOC_PATH = "/redoc"

SchemaProvider = Callable[[], dict[str, Any]]


def docs_router(title: str, schema: SchemaProvider, authenticator: Authenticator) -> APIRouter:
    """Documentation routes; every one requires an API key as Basic-auth password."""
    router = APIRouter(include_in_schema=False, dependencies=[Depends(basic_guard(authenticator))])

    router.add_api_route(OPENAPI_PATH, lambda: JSONResponse(schema()), methods=["GET"])

    @router.get(SWAGGER_PATH)
    def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=OPENAPI_PATH, title=title, oauth2_redirect_url=OAUTH2_REDIRECT_PATH
        )

    @router.get(OAUTH2_REDIRECT_PATH)
    def swagger_oauth2_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    @router.get(REDOC_PATH)
    def redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url=OPENAPI_PATH, title=title)

    return router
