import os
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, HttpUrl

from app.auth import verify_api_key, verify_basic_auth
from app.browser_pool import BrowserPool
from app.renderer import render, NavigationError, RenderTimeoutError

_pool: BrowserPool | None = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = BrowserPool(
        size=int(os.environ.get("MAX_WORKERS", "3")),
        headless=_env_bool("HEADLESS", False),
    )
    yield
    _pool.shutdown()


app = FastAPI(lifespan=lifespan)


class RenderRequest(BaseModel):
    url: HttpUrl
    wait_for: str | None = None
    timeout: int = 30


@app.post("/render", dependencies=[Depends(verify_api_key)])
def render_url(req: RenderRequest):
    driver = _pool.acquire()
    if driver is None:
        raise HTTPException(status_code=503, detail={"error": "pool_exhausted"})
    try:
        html = render(driver, str(req.url), req.wait_for, req.timeout)
        return Response(content=html, media_type="text/html")
    except NavigationError as exc:
        raise HTTPException(status_code=502, detail={"error": "navigation_failed", "detail": str(exc)})
    except RenderTimeoutError as exc:
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
  <iframe src="http://localhost:{vnc_port}/vnc.html?autoconnect=true&resize=scale"></iframe>
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
