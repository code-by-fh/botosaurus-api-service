"""FastAPI application: routes, lifecycle and response mapping.

Start with ``uvicorn --factory app.main:create_app``.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response

import app.logging_config  # noqa: F401 -- configures logging on import
from app.api_models import RenderRequest
from app.auth import basic_guard, bearer_guard
from app.config import Settings, load_settings
from app.content.output import OutputSpec, build_output
from app.errors import install_error_handling
from app.openapi_docs import (
    TRACE_REQUEST_PARAMETER,
    UNAUTHORIZED_RESPONSE,
    install_openapi,
    render_responses,
)
from app.runtime import Runtime, start_runtime
from app.scraping.scraper import ScrapeRequest
from app.novnc_proxy import novnc_proxy_router
from app.vnc import vnc_page

log = logging.getLogger("render.api")

SERVICE_VERSION = "2.0.0"
API_PREFIX = "/api/v1"
URL_SAFE_CHARACTERS = ":/?#[]@!$&'()*+,;=%~"

RuntimeStarter = Callable[[Settings], Awaitable[Runtime]]


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _to_scrape_request(body: RenderRequest) -> ScrapeRequest:
    return ScrapeRequest(
        url=str(body.url),
        mode=body.mode,
        wait_for=body.wait_for,
        selector=body.selector,
        timeout_seconds=float(body.timeout),
        use_proxy=body.use_proxy,
        block_resources=body.block_resources,
        idle_timeout_seconds=float(body.idle_timeout) if body.idle_timeout else None,
    )


async def _render(body: RenderRequest, runtime: Runtime) -> Response:
    started = time.monotonic()
    result = await runtime.scraper.scrape(_to_scrape_request(body))
    output = await asyncio.to_thread(
        build_output, result.html, OutputSpec(body.selector, body.format)
    )
    log.info(
        "Rendered %s via %s in %.2fs",
        body.url,
        result.engine,
        time.monotonic() - started,
    )
    headers = {
        "X-Render-Engine": result.engine,
        "X-Render-Stable": str(result.stable).lower(),
        "X-Final-Url": quote(result.final_url, safe=URL_SAFE_CHARACTERS),
        "X-Upstream-Status": str(result.upstream_status),
    }
    return Response(content=output.content, media_type=output.media_type, headers=headers)


def _health_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/health", summary="Liveness probe (no authentication)")
    def health() -> dict:
        return {"status": "ok"}

    @router.get(
        "/health/detail",
        summary="Pool utilisation and learned HTTP verdicts",
        dependencies=[Depends(bearer_guard(settings.api_keys))],
        responses={401: UNAUTHORIZED_RESPONSE},
    )
    def health_detail(request: Request) -> dict:
        runtime = _runtime(request)
        return {
            "status": "ok",
            "version": SERVICE_VERSION,
            "pool": vars(runtime.pool.stats()),
            "verdicts": runtime.verdicts.counts(),
        }

    return router


def _render_router(settings: Settings) -> APIRouter:
    router = APIRouter(dependencies=[Depends(bearer_guard(settings.api_keys))])

    @router.post(
        f"{API_PREFIX}/render",
        summary="Render a URL and return HTML or Markdown",
        response_class=Response,
        responses=render_responses(),
        openapi_extra=TRACE_REQUEST_PARAMETER,
    )
    async def render(body: RenderRequest, request: Request) -> Response:
        return await _render(body, _runtime(request))

    return router


def _vnc_router(settings: Settings) -> APIRouter:
    router = APIRouter(dependencies=[Depends(basic_guard(settings.api_keys))])

    @router.get(
        "/vnc",
        summary="Live view of the headed browsers (requires ENABLE_VNC)",
        response_class=Response,
        responses={
            200: {"description": "Viewer page", "content": {"text/html": {}}},
            401: UNAUTHORIZED_RESPONSE,
            404: {"description": "`NOT_FOUND`: VNC is disabled"},
        },
    )
    def vnc(request: Request) -> Response:
        if not settings.vnc_enabled:
            raise HTTPException(status_code=404, detail="VNC is disabled")
        host = request.url.hostname or "localhost"
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        return Response(content=vnc_page(host, scheme), media_type="text/html")

    return router


def create_app(
    settings: Settings | None = None, runtime_starter: RuntimeStarter = start_runtime
) -> FastAPI:
    """Build the application.

    :param settings: configuration; read from the environment when omitted.
    :param runtime_starter: creates the components on startup (replaced in tests).
    :raises ConfigError: if the environment configuration is invalid.
    """
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.runtime = await runtime_starter(resolved)
        log.info("render-api-service %s ready", SERVICE_VERSION)
        yield
        await application.state.runtime.close()

    application = FastAPI(title="render-api-service", version=SERVICE_VERSION, lifespan=lifespan)
    install_error_handling(application)
    install_openapi(application)
    for router in (
        _health_router(resolved),
        _render_router(resolved),
        _vnc_router(resolved),
    ):
        application.include_router(router)
    novnc_prefix = os.environ.get("NOVNC_PREFIX", "").strip()
    if novnc_prefix:
        log.info("noVNC proxy mounted at %s", novnc_prefix)
        application.include_router(novnc_proxy_router(novnc_prefix, resolved.vnc_enabled))
    return application
