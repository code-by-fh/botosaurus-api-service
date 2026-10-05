"""FastAPI application: routes, lifecycle and response mapping.

Start with ``uvicorn --factory app.main:create_app``.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Body, Depends, FastAPI, Request, Response

from app.api_models import RenderRequest
from app.auth import Authenticator, bearer_guard
from app.auth_throttle import FailedAuthLimiter
from app.body_limit import BodyLimitMiddleware
from app.config import Settings, load_settings
from app.content.output import OutputSpec, build_output
from app.docs_routes import docs_router
from app.errors import ServiceError, install_error_handling, trace_id_of
from app.log_safety import loggable_url
from app.logging_config import setup_logging
from app.openapi_docs import (
    AUTH_RESPONSES,
    RENDER_REQUEST_EXAMPLES,
    TRACE_REQUEST_PARAMETER,
    install_openapi,
    render_responses,
)
from app.runtime import Runtime, start_runtime
from app.scraping.clearance import ClearanceStore
from app.scraping.scraper import ScrapeRequest, ScrapeResult
from app.security_headers import install_security_headers
from app.timing import Phase, PhaseTimer
from app.vnc import VncAccess, vnc_router
from app.vnc_proxy import VncUpstream
from app.vnc_session import SessionSigner

log = logging.getLogger("render.api")

SERVICE_NAME = "page-render-service"
SERVICE_VERSION = "2.0.0"
API_PREFIX = "/api/v1"
URL_SAFE_CHARACTERS = ":/?#[]@!$&'()*+,;=%~"
OUTCOME_OK = "ok"
OUTCOME_UNEXPECTED = "INTERNAL_ERROR"
NO_ENGINE = "none"

RuntimeStarter = Callable[[Settings], Awaitable[Runtime]]


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _to_scrape_request(body: RenderRequest, timer: PhaseTimer) -> ScrapeRequest:
    return ScrapeRequest(
        url=str(body.url),
        mode=body.mode,
        wait_for=body.wait_for,
        selector=body.selector,
        timeout_seconds=float(body.timeout),
        use_proxy=body.use_proxy,
        block_resources=body.blocked_resources,
        timer=timer,
    )


class _RenderLog:
    """What the per-request timing line reports besides the phase durations."""

    def __init__(self, body: RenderRequest, trace_id: str):
        self.body = body
        self.trace_id = trace_id
        self.outcome = OUTCOME_UNEXPECTED
        self.engine = NO_ENGINE

    def write(self, timer: PhaseTimer) -> None:
        log.info(
            "Render timing traceId=%s url=%s outcome=%s engine=%s %s",
            self.trace_id,
            loggable_url(self.body.url),
            self.outcome,
            self.engine,
            timer.summary(),
        )


async def _render(body: RenderRequest, runtime: Runtime, trace_id: str) -> Response:
    # Logged for failures too: a slow TARGET_BLOCKED or TIMEOUT is exactly the
    # case whose time needs explaining.
    timer = PhaseTimer()
    render_log = _RenderLog(body, trace_id)
    try:
        result = await runtime.scraper.scrape(_to_scrape_request(body, timer))
        render_log.engine = result.engine
        response = await _respond(body, result, timer)
        render_log.outcome = OUTCOME_OK
        return response
    except ServiceError as exc:
        render_log.outcome = exc.code
        raise
    finally:
        render_log.write(timer)


async def _respond(body: RenderRequest, result: ScrapeResult, timer: PhaseTimer) -> Response:
    with timer.phase(Phase.OUTPUT):
        output = await asyncio.to_thread(
            build_output, result.html, OutputSpec(body.selector, body.format)
        )
    headers = {
        "X-Render-Engine": result.engine,
        "X-Render-Stable": str(result.stable).lower(),
        "X-Render-Ready-Reason": result.ready_reason,
        "X-Render-Profile": result.profile,
        "X-Render-Route": result.route,
        "X-Final-Url": quote(_without_userinfo(result.final_url), safe=URL_SAFE_CHARACTERS),
        "X-Upstream-Status": str(result.upstream_status),
    }
    return Response(content=output.content, media_type=output.media_type, headers=headers)


def _without_userinfo(url: str) -> str:
    # Credentials in a redirect target belong to the target, not in a response header.
    parts = urlsplit(url)
    host_and_port = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=host_and_port))


def _clearance_stats(store: ClearanceStore | None) -> dict:
    # Only the count: cookie names, hosts and values stay out of every response.
    return {"enabled": store is not None, "entries": store.count() if store else 0}


def _health_details(runtime: Runtime) -> dict:
    return {
        "status": "ok",
        "version": SERVICE_VERSION,
        "pool": vars(runtime.pool.stats()),
        "verdicts": runtime.verdicts.counts(),
        "clearance": _clearance_stats(runtime.clearance),
        "profiles": {"entries": runtime.profiles.count()},
        "proxy_hosts": {"entries": runtime.proxy_hosts.count()},
    }


def _health_router(authenticator: Authenticator) -> APIRouter:
    router = APIRouter(tags=["Health"])

    @router.get("/health", summary="Liveness probe (no authentication)")
    def health() -> dict:
        return {"status": "ok"}

    @router.get(
        "/health/detail",
        summary=(
            "Pool utilisation, learned verdicts and profiles, stored clearance count, "
            "hosts remembered as needing HOME_PROXY"
        ),
        dependencies=[Depends(bearer_guard(authenticator))],
        responses=AUTH_RESPONSES,
    )
    def health_detail(request: Request) -> dict:
        return _health_details(_runtime(request))

    return router


def _render_router(authenticator: Authenticator) -> APIRouter:
    router = APIRouter(tags=["Render"], dependencies=[Depends(bearer_guard(authenticator))])

    @router.post(
        f"{API_PREFIX}/render",
        summary="Render a URL and return HTML or Markdown",
        response_class=Response,
        responses=render_responses(),
        openapi_extra=TRACE_REQUEST_PARAMETER,
    )
    async def render(
        body: Annotated[RenderRequest, Body(openapi_examples=RENDER_REQUEST_EXAMPLES)],
        request: Request,
    ) -> Response:
        return await _render(body, _runtime(request), trace_id_of(request))

    return router


def _include_routers(application: FastAPI, settings: Settings) -> None:
    access = settings.access
    authenticator = Authenticator(settings.api_keys, FailedAuthLimiter(), access.trusted_proxies)
    application.include_router(_health_router(authenticator))
    application.include_router(_render_router(authenticator))
    if access.docs_enabled:
        application.include_router(
            docs_router(application.title, application.openapi, authenticator)
        )
    if access.vnc_enabled:
        vnc = VncAccess(authenticator, SessionSigner(), VncUpstream(access.vnc_port))
        application.include_router(vnc_router(vnc))


def _new_application(lifespan: Callable) -> FastAPI:
    # The built-in documentation routes are public; docs_routes replaces them.
    return FastAPI(
        title=SERVICE_NAME,
        version=SERVICE_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )


def create_app(
    settings: Settings | None = None, runtime_starter: RuntimeStarter = start_runtime
) -> FastAPI:
    """Build the application.

    :param settings: configuration; read from the environment when omitted.
    :param runtime_starter: creates the components on startup (replaced in tests).
    :raises ConfigError: if the environment configuration is invalid.
    """
    resolved = settings or load_settings()
    setup_logging(resolved.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.runtime = await runtime_starter(resolved)
        log.info("%s %s ready", SERVICE_NAME, SERVICE_VERSION)
        yield
        await application.state.runtime.close()

    application = _new_application(lifespan)
    # Added first, so it runs innermost: its 413 still passes the trace-id and
    # security-header middlewares.
    application.add_middleware(BodyLimitMiddleware)
    install_error_handling(application)
    install_security_headers(application)
    install_openapi(application)
    _include_routers(application, resolved)
    return application
