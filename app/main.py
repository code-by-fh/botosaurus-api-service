import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Literal

from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, HTTPException, Response
from markdownify import markdownify
from pydantic import BaseModel, HttpUrl

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
    timeout: int = 30
    format: Literal["html", "markdown"] = "html"


@app.post("/render", dependencies=[Depends(verify_api_key)])
def render_url(req: RenderRequest):
    log.debug(
        "POST /render url=%s format=%s wait_for=%s timeout=%d",
        req.url, req.format, req.wait_for, req.timeout,
    )
    driver = _pool.acquire()
    if driver is None:
        raise HTTPException(status_code=503, detail={"error": "pool_exhausted"})
    t0 = time.monotonic()
    try:
        html = render(driver, str(req.url), req.wait_for, req.timeout)
        if req.format == "markdown":
            soup = BeautifulSoup(html, "html.parser")
            body_html = str(soup.body) if soup.body else html
            md = markdownify(body_html, heading_style="ATX", strip=["img", "script", "style"]).strip()
            elapsed = time.monotonic() - t0
            log.info("Rendered %s as markdown in %.2fs", req.url, elapsed)
            return Response(content=md, media_type="text/markdown; charset=utf-8")
        elapsed = time.monotonic() - t0
        log.info("Rendered %s as html in %.2fs", req.url, elapsed)
        return Response(content=html, media_type="text/html")
    except NavigationError as exc:
        log.error("Navigation failed for %s: %s", req.url, exc)
        raise HTTPException(status_code=502, detail={"error": "navigation_failed", "detail": str(exc)})
    except RenderTimeoutError as exc:
        log.error("Render timeout for %s: %s", req.url, exc)
        raise HTTPException(status_code=504, detail={"error": "timeout", "detail": str(exc)})
    finally:
        _pool.release(driver)


def _vnc_page() -> str:
    vnc_port = os.environ.get("VNC_PORT", "6080")
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
  <iframe src="http://localhost:{vnc_port}/vnc.html?autoconnect=true&resize=scale&path=websockify"></iframe>
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
def vnc():
    return Response(content=_vnc_page(), media_type="text/html")
