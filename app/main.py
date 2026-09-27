import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Literal

from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from markdownify import markdownify
from pydantic import AliasChoices, BaseModel, Field, HttpUrl


import app.logging_config  # noqa: F401 — triggers logging setup on import
from app.auth import verify_api_key, verify_basic_auth
from app.browser_pool import BrowserPool
from app.renderer import render, NavigationError, RenderTimeoutError

log = logging.getLogger("botosaurus.api")

_pool: BrowserPool | None = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    log.info("Starting up botosaurus-api-service")
    _pool = BrowserPool(
        size=int(os.environ.get("MAX_WORKERS", "3")),
        headless=_env_bool("HEADLESS", False),
    )
    yield
    log.info("Shutting down botosaurus-api-service")
    _pool.shutdown()


app = FastAPI(lifespan=lifespan)


class RenderRequest(BaseModel):
    url: HttpUrl
    wait_for: str | None = None
    selector: str | None = Field(
        default=None,
        validation_alias=AliasChoices("selector", "element", "target"),
        description="Optional CSS selector of DOM element to extract and return",
    )
    timeout: int = 30
    format: Literal["html", "markdown"] = "html"
    use_proxy: bool = False


@app.post("/render", dependencies=[Depends(verify_api_key)])
def render_url(req: RenderRequest):
    log.debug(
        "POST /render url=%s format=%s selector=%s wait_for=%s timeout=%d use_proxy=%s",
        req.url, req.format, req.selector, req.wait_for, req.timeout, req.use_proxy,
    )
    proxy_url = None
    if req.use_proxy:
        proxy_url = os.environ.get("HOME_PROXY") or os.environ.get("PROXY")
        if not proxy_url:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "proxy_not_configured",
                    "detail": "use_proxy is true, but HOME_PROXY environment variable is not set",
                },
            )

    driver, is_on_demand = _pool.acquire(proxy=proxy_url)
    if driver is None:
        raise HTTPException(status_code=503, detail={"error": "pool_exhausted"})
    t0 = time.monotonic()
    try:
        html = render(driver, str(req.url), req.wait_for, req.timeout)
        soup = BeautifulSoup(html, "html.parser")

        if req.selector:
            matched = soup.select_one(req.selector)
            if not matched:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": "element_not_found",
                        "detail": f"Element matching selector '{req.selector}' was not found",
                    },
                )
            target_html = str(matched)
        elif req.format == "markdown":
            target_html = str(soup.body) if soup.body else html
        else:
            target_html = html

        if req.format == "markdown":
            md = markdownify(target_html, heading_style="ATX", strip=["img", "script", "style"]).strip()
            elapsed = time.monotonic() - t0
            log.info("Rendered %s as markdown in %.2fs", req.url, elapsed)
            return Response(content=md, media_type="text/markdown; charset=utf-8")

        elapsed = time.monotonic() - t0
        log.info("Rendered %s as html in %.2fs", req.url, elapsed)
        return Response(content=target_html, media_type="text/html")
    except NavigationError as exc:
        log.error("Navigation failed for %s: %s", req.url, exc)
        raise HTTPException(status_code=502, detail={"error": "navigation_failed", "detail": str(exc)})
    except RenderTimeoutError as exc:
        log.error("Render timeout for %s: %s", req.url, exc)
        raise HTTPException(status_code=504, detail={"error": "timeout", "detail": str(exc)})
    finally:
        _pool.release(driver, is_on_demand=is_on_demand)


def _vnc_page(host: str = "localhost", scheme: str = "http") -> str:
    prefix = os.environ.get("NOVNC_PREFIX", "").strip()
    if prefix:
        clean_prefix = "/" + prefix.strip("/")
        ws_path = clean_prefix.lstrip("/") + "/websockify"
        src = f"{clean_prefix}/vnc.html?autoconnect=true&resize=scale&path={ws_path}"
    else:
        vnc_port = os.environ.get("VNC_PORT", "6080")
        src = f"{scheme}://{host}:{vnc_port}/vnc.html?autoconnect=true&resize=scale&path=websockify"

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Browser View — noVNC</title>
  <style>
    body, html {{ margin: 0; padding: 0; height: 100%; background: #1a1a1a; }}
    iframe {{ width: 100%; height: 100%; border: none; display: block; }}
  </style>
</head>
<body>
  <iframe src="{src}"></iframe>
</body>
</html>"""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/detail", dependencies=[Depends(verify_api_key)])
def health_detail():
    return {
        "status": "ok",
        "workers_busy": _pool.busy,
        "workers_total": _pool.total,
    }


@app.get("/vnc", dependencies=[Depends(verify_basic_auth)])
def vnc(request: Request):
    host = request.url.hostname or "localhost"
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return Response(content=_vnc_page(host=host, scheme=scheme), media_type="text/html")
