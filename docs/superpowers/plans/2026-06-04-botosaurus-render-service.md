# Botosaurus Render Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Docker service exposing a synchronous `POST /render` API that navigates a URL with Botosaurus' AntiDetect Chrome, waits for the DOM, and returns the full rendered HTML.

**Architecture:** FastAPI app with a warm `BrowserPool` (N `botasaurus_driver.Driver` instances, managed via `queue.Queue` + `threading.Semaphore`). All browsers run headed on an Xvfb virtual display (:99), visible live via NoVNC at port 6080. Startup sequence in `entrypoint.sh`: Xvfb → x11vnc → websockify/noVNC → uvicorn.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, botasaurus-driver, Selenium WebDriverWait, Docker (nikolaik/python-nodejs base), Xvfb, x11vnc, noVNC, websockify, pytest, httpx

---

## File Map

| File | Responsibility |
|---|---|
| `app/__init__.py` | Package marker |
| `app/renderer.py` | `render(driver, url, wait_for, timeout) -> str` — navigate + wait + return page_source |
| `app/browser_pool.py` | `BrowserPool` — warm pool of Driver instances, acquire/release |
| `app/main.py` | FastAPI app, lifespan, POST /render, GET /health, GET /vnc |
| `tests/__init__.py` | Package marker |
| `tests/test_renderer.py` | Unit tests for renderer.py |
| `tests/test_browser_pool.py` | Unit tests for browser_pool.py |
| `tests/test_api.py` | Unit tests for API endpoints (TestClient, pool mocked) |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Image: base + Chrome + Xvfb + x11vnc + noVNC + app |
| `entrypoint.sh` | Start Xvfb, x11vnc, websockify, uvicorn |
| `docker-compose.yml` | Service definition, port mapping, env vars |

---

## Task 1: Project Scaffold

**Files:**
- Create: `requirements.txt`
- Create: `app/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Initialize git repository**

```bash
cd C:\Development\botosaurus-api-service
git init
```

Expected: `Initialized empty Git repository`

- [ ] **Step 2: Create requirements.txt**

```
botasaurus-driver>=3.0.0
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
pydantic>=2.0.0
pytest>=8.0.0
httpx>=0.27.0
websockify>=0.11.0
```

- [ ] **Step 3: Create package markers**

`app/__init__.py` — empty file.  
`tests/__init__.py` — empty file.

- [ ] **Step 4: Install dependencies locally (for test runner)**

```bash
pip install -r requirements.txt
```

Expected: all packages install without error.

- [ ] **Step 5: Commit scaffold**

```bash
git add requirements.txt app/__init__.py tests/__init__.py
git commit -m "chore: project scaffold"
```

---

## Task 2: renderer.py

**Files:**
- Create: `app/renderer.py`
- Create: `tests/test_renderer.py`

The renderer has two custom exceptions and one function. `render()` calls `driver.get(url)`, then waits either for a CSS selector or for `document.readyState == "complete"`, then returns `driver.page_source`.

- [ ] **Step 1: Write the failing tests**

`tests/test_renderer.py`:

```python
from unittest.mock import MagicMock, patch, call
import pytest
from selenium.common.exceptions import TimeoutException, WebDriverException
from app.renderer import render, NavigationError, RenderTimeoutError


def _driver(page_source="<html><body>ok</body></html>"):
    d = MagicMock()
    d.page_source = page_source
    return d


@patch("app.renderer.WebDriverWait")
def test_render_returns_page_source(mock_wdw):
    driver = _driver("<html>test</html>")
    mock_wdw.return_value.until.return_value = True
    result = render(driver, "https://example.com", timeout=5)
    assert result == "<html>test</html>"
    driver.get.assert_called_once_with("https://example.com")


@patch("app.renderer.WebDriverWait")
def test_render_passes_wait_for_selector(mock_wdw):
    driver = _driver()
    mock_wdw.return_value.until.return_value = True
    render(driver, "https://example.com", wait_for="#main", timeout=5)
    # WebDriverWait is constructed with (driver, 5)
    mock_wdw.assert_called_once_with(driver, 5)


@patch("app.renderer.WebDriverWait")
def test_render_timeout_raises_render_timeout_error(mock_wdw):
    driver = _driver()
    mock_wdw.return_value.until.side_effect = TimeoutException()
    with pytest.raises(RenderTimeoutError):
        render(driver, "https://example.com", timeout=1)


def test_render_navigation_error_raises_navigation_error():
    driver = _driver()
    driver.get.side_effect = WebDriverException("net::ERR_NAME_NOT_RESOLVED")
    with pytest.raises(NavigationError):
        render(driver, "https://does-not-exist.invalid")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_renderer.py -v
```

Expected: `ImportError` — `app.renderer` does not exist yet.

- [ ] **Step 3: Implement renderer.py**

`app/renderer.py`:

```python
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, WebDriverException


class NavigationError(Exception):
    pass


class RenderTimeoutError(Exception):
    pass


def render(driver, url: str, wait_for: str | None = None, timeout: int = 30) -> str:
    try:
        driver.get(url)
    except WebDriverException as exc:
        raise NavigationError(str(exc)) from exc

    try:
        if wait_for:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_for))
            )
        else:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
    except TimeoutException as exc:
        target = wait_for or "readyState==complete"
        raise RenderTimeoutError(
            f"Timed out after {timeout}s waiting for '{target}'"
        ) from exc

    return driver.page_source
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_renderer.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/renderer.py tests/test_renderer.py
git commit -m "feat: add renderer with wait_for and timeout support"
```

---

## Task 3: browser_pool.py

**Files:**
- Create: `app/browser_pool.py`
- Create: `tests/test_browser_pool.py`

The pool holds N drivers in a `queue.Queue`. `acquire()` is non-blocking: returns a driver or `None` immediately if all are busy. `release()` puts the driver back. `shutdown()` closes all drivers.

- [ ] **Step 1: Write the failing tests**

`tests/test_browser_pool.py`:

```python
from unittest.mock import MagicMock, patch
import pytest
from app.browser_pool import BrowserPool


def _make_pool(size=2):
    mock_driver_cls = MagicMock()
    mock_driver_cls.side_effect = [MagicMock() for _ in range(size)]
    with patch("app.browser_pool.Driver", mock_driver_cls):
        pool = BrowserPool(size=size)
    return pool, mock_driver_cls


def test_pool_total_matches_size():
    pool, _ = _make_pool(size=2)
    assert pool.total == 2


def test_acquire_returns_driver():
    pool, _ = _make_pool(size=1)
    driver = pool.acquire()
    assert driver is not None


def test_acquire_increments_busy():
    pool, _ = _make_pool(size=2)
    pool.acquire()
    assert pool.busy == 1


def test_acquire_returns_none_when_pool_exhausted():
    pool, _ = _make_pool(size=1)
    pool.acquire()  # exhaust
    assert pool.acquire() is None


def test_release_decrements_busy():
    pool, _ = _make_pool(size=1)
    driver = pool.acquire()
    pool.release(driver)
    assert pool.busy == 0


def test_release_makes_driver_available_again():
    pool, _ = _make_pool(size=1)
    driver = pool.acquire()
    pool.release(driver)
    assert pool.acquire() is not None


def test_shutdown_closes_all_drivers():
    pool, _ = _make_pool(size=2)
    drivers = [pool.acquire(), pool.acquire()]
    for d in drivers:
        pool.release(d)
    pool.shutdown()
    for d in drivers:
        d.close.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_browser_pool.py -v
```

Expected: `ImportError` — `app.browser_pool` does not exist yet.

- [ ] **Step 3: Implement browser_pool.py**

`app/browser_pool.py`:

```python
import queue
import threading
from botasaurus_driver import Driver


class BrowserPool:
    def __init__(self, size: int):
        self._size = size
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._busy = 0
        for _ in range(size):
            self._queue.put(Driver(headless=False))

    @property
    def total(self) -> int:
        return self._size

    @property
    def busy(self) -> int:
        with self._lock:
            return self._busy

    def acquire(self):
        try:
            driver = self._queue.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy += 1
        return driver

    def release(self, driver) -> None:
        with self._lock:
            self._busy -= 1
        self._queue.put(driver)

    def shutdown(self) -> None:
        while True:
            try:
                driver = self._queue.get_nowait()
                driver.close()
            except queue.Empty:
                break
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_browser_pool.py -v
```

Expected: 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/browser_pool.py tests/test_browser_pool.py
git commit -m "feat: add warm BrowserPool with acquire/release/shutdown"
```

---

## Task 4: main.py — POST /render endpoint

**Files:**
- Create: `app/main.py`
- Create: `tests/test_api.py`

The lifespan initializes `BrowserPool` from `MAX_WORKERS` env var. `POST /render` acquires a driver, calls `renderer.render()`, always releases in `finally`, returns `text/html` or raises HTTP errors.

- [ ] **Step 1: Write the failing tests**

`tests/test_api.py`:

```python
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
import app.main as main_module
from app.renderer import NavigationError, RenderTimeoutError


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    pool.acquire.return_value = MagicMock()
    pool.busy = 1
    pool.total = 3
    return pool


@pytest.fixture
def client(mock_pool):
    with patch.object(main_module, "_pool", mock_pool):
        with TestClient(app) as c:
            yield c, mock_pool


def test_render_returns_html(client, mock_pool):
    c, _ = client
    with patch("app.main.render", return_value="<html>hello</html>"):
        resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text == "<html>hello</html>"


def test_render_503_when_pool_exhausted(client, mock_pool):
    c, pool = client
    pool.acquire.return_value = None
    resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "pool_exhausted"


def test_render_422_on_missing_url(client):
    c, _ = client
    resp = c.post("/render", json={})
    assert resp.status_code == 422


def test_render_502_on_navigation_error(client):
    c, _ = client
    with patch("app.main.render", side_effect=NavigationError("failed")):
        resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "navigation_failed"


def test_render_504_on_timeout(client):
    c, _ = client
    with patch("app.main.render", side_effect=RenderTimeoutError("timed out")):
        resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 504
    assert resp.json()["detail"]["error"] == "timeout"


def test_render_releases_driver_on_error(client, mock_pool):
    c, pool = client
    with patch("app.main.render", side_effect=NavigationError("oops")):
        c.post("/render", json={"url": "https://example.com"})
    pool.release.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_api.py -v
```

Expected: `ImportError` — `app.main` does not exist yet.

- [ ] **Step 3: Implement app/main.py (render endpoint only)**

`app/main.py`:

```python
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, HttpUrl

from app.browser_pool import BrowserPool
from app.renderer import render, NavigationError, RenderTimeoutError

_pool: BrowserPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = BrowserPool(size=int(os.environ.get("MAX_WORKERS", "3")))
    yield
    _pool.shutdown()


app = FastAPI(lifespan=lifespan)


class RenderRequest(BaseModel):
    url: HttpUrl
    wait_for: str | None = None
    timeout: int = 30


@app.post("/render")
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_api.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat: add POST /render endpoint with pool integration"
```

---

## Task 5: main.py — GET /health and GET /vnc endpoints

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
def test_health_returns_pool_status(client, mock_pool):
    c, pool = client
    pool.busy = 2
    pool.total = 3
    resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["workers_total"] == 3


def test_vnc_returns_html_with_iframe(client):
    c, _ = client
    resp = c.get("/vnc")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "iframe" in resp.text
    assert "6080" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_api.py::test_health_returns_pool_status tests/test_api.py::test_vnc_returns_html_with_iframe -v
```

Expected: FAIL — endpoints not defined yet.

- [ ] **Step 3: Add /health and /vnc to app/main.py**

Append after the `/render` route in `app/main.py`:

```python
_VNC_PAGE = """<!DOCTYPE html>
<html>
<head>
  <title>Browser View — noVNC</title>
  <style>
    body, html { margin: 0; padding: 0; height: 100%; background: #1a1a1a; }
    iframe { width: 100%; height: 100%; border: none; display: block; }
  </style>
</head>
<body>
  <iframe src="http://localhost:6080/vnc.html?autoconnect=true&resize=scale"></iframe>
</body>
</html>"""


@app.get("/health")
def health():
    return {
        "status": "ok",
        "workers_busy": _pool.busy,
        "workers_total": _pool.total,
    }


@app.get("/vnc")
def vnc():
    return Response(content=_VNC_PAGE, media_type="text/html")
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/ -v
```

Expected: all tests PASS (renderer: 4, pool: 8, api: 8 = 20 total).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat: add GET /health and GET /vnc endpoints"
```

---

## Task 6: Dockerfile

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Create .dockerignore**

`.dockerignore`:

```
__pycache__
*.pyc
*.pyo
.pytest_cache
tests/
docs/
.git
```

- [ ] **Step 2: Create Dockerfile**

`Dockerfile`:

```dockerfile
FROM nikolaik/python-nodejs:python3.12-nodejs18-slim

# System dependencies: Chrome, Xvfb, VNC, download tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg2 \
    ca-certificates \
    apt-transport-https \
    xvfb \
    x11vnc \
    x11-utils \
    xdg-utils \
    lsof \
    git \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome stable
RUN wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
       http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# noVNC (web VNC client)
RUN git clone --depth 1 --branch v1.4.0 \
    https://github.com/novnc/noVNC /opt/novnc \
    && git clone --depth 1 \
    https://github.com/novnc/websockify /opt/novnc/utils/websockify \
    && ln -s /opt/novnc/utils/websockify/run /opt/novnc/utils/launch.sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DISPLAY=:99
ENV CHROME_BIN=/usr/bin/google-chrome

EXPOSE 8000 6080

ENTRYPOINT ["/entrypoint.sh"]
```

- [ ] **Step 3: Verify Dockerfile syntax**

```bash
docker build --no-cache -t botosaurus-api-service . 2>&1 | tail -20
```

Expected: `Successfully built <id>` with no errors. Fix any package/layer errors before continuing.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "feat: add Dockerfile with Chrome, Xvfb, x11vnc, noVNC"
```

---

## Task 7: entrypoint.sh + docker-compose.yml

**Files:**
- Create: `entrypoint.sh`
- Create: `docker-compose.yml`

- [ ] **Step 1: Create entrypoint.sh**

`entrypoint.sh`:

```bash
#!/bin/bash
set -e

echo "[entrypoint] Starting Xvfb on display :99"
Xvfb :99 -screen 0 1280x720x24 -ac &
XVFB_PID=$!
sleep 1

echo "[entrypoint] Starting x11vnc on :5900"
x11vnc -display :99 -forever -nopw -quiet -rfbport 5900 &
sleep 0.5

echo "[entrypoint] Starting noVNC websockify on :6080"
/opt/novnc/utils/websockify --web /opt/novnc 6080 localhost:5900 &
sleep 0.5

echo "[entrypoint] Starting FastAPI on :8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 2: Create docker-compose.yml**

`docker-compose.yml`:

```yaml
services:
  botosaurus-api:
    build: .
    ports:
      - "8000:8000"
      - "6080:6080"
    environment:
      MAX_WORKERS: "3"
      DISPLAY: ":99"
    restart: unless-stopped
    shm_size: "2gb"
```

The `shm_size: "2gb"` is required — Chrome crashes without sufficient shared memory in Docker.

- [ ] **Step 3: Run the full stack**

```bash
docker-compose up --build
```

Watch for these log lines in order:
```
[entrypoint] Starting Xvfb on display :99
[entrypoint] Starting x11vnc on :5900
[entrypoint] Starting noVNC websockify on :6080
[entrypoint] Starting FastAPI on :8000
INFO:     Application startup complete.
```

If Chrome crashes with "DevToolsActivePort file doesn't exist", add `--no-sandbox` flag — see Step 4.

- [ ] **Step 4: Smoke test the API**

In a second terminal:

```bash
curl -s -X POST http://localhost:8000/render \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "timeout": 30}' \
  | head -c 200
```

Expected: `<!doctype html><html>...` (rendered HTML from example.com).

```bash
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok","workers_busy":0,"workers_total":3}`

- [ ] **Step 5: Verify noVNC**

Open `http://localhost:6080/vnc.html` in a browser. You should see the virtual desktop with Chrome windows.

Open `http://localhost:8000/vnc` — should show an embedded iframe with the same view.

- [ ] **Step 6: Handle Chrome sandbox (if needed)**

If Chrome fails with sandbox errors in Step 3, modify `app/browser_pool.py` — pass Chrome options:

```python
from selenium.webdriver.chrome.options import Options as ChromeOptions

def _make_driver() -> Driver:
    options = ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return Driver(headless=False, chrome_options=options)
```

Replace `Driver(headless=False)` with `_make_driver()` in `BrowserPool.__init__`.

Note: Check `botasaurus_driver.Driver` constructor signature first — parameter names may differ (`options` vs `chrome_options`). Run `python -c "from botasaurus_driver import Driver; help(Driver.__init__)"` inside the container to verify.

- [ ] **Step 7: Commit**

```bash
git add entrypoint.sh docker-compose.yml app/browser_pool.py
git commit -m "feat: add entrypoint.sh and docker-compose with noVNC"
```

---

## Task 8: Final Verification

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -v
```

Expected: 20 tests PASS, 0 FAIL.

- [ ] **Step 2: Test wait_for parameter**

```bash
curl -s -X POST http://localhost:8000/render \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "wait_for": "h1", "timeout": 15}' \
  | grep -o "<h1>.*</h1>"
```

Expected: `<h1>Example Domain</h1>` (or similar).

- [ ] **Step 3: Test pool exhaustion (503)**

With `MAX_WORKERS=1`, send 2 simultaneous requests:

```bash
# Set MAX_WORKERS=1 in docker-compose.yml, rebuild
curl -s -X POST http://localhost:8000/render \
  -H "Content-Type: application/json" \
  -d '{"url": "https://httpbin.org/delay/5", "timeout": 10}' &

curl -s -X POST http://localhost:8000/render \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}' | python -m json.tool
```

Expected: second request returns `{"detail": {"error": "pool_exhausted"}}` with status 503.

- [ ] **Step 4: Test timeout (504)**

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/render \
  -H "Content-Type: application/json" \
  -d '{"url": "https://httpbin.org/delay/10", "wait_for": "#never-exists", "timeout": 2}'
```

Expected: `504`.

- [ ] **Step 5: Tag release**

```bash
git tag v1.0.0
```

---

## Chrome Sandbox Note

Chrome inside Docker requires either `--no-sandbox` (when not using user namespaces) or running with `--privileged`. The `shm_size: "2gb"` in docker-compose handles the `/dev/shm` crash. If you still see Chrome crashes, the fix is in Task 7 Step 6.
