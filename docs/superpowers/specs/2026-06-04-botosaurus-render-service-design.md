# Botosaurus Render Service — Design Spec

**Date:** 2026-06-04  
**Status:** Approved

## Overview

A Docker-based HTTP service that accepts a URL, renders it in a real Chromium browser via Botosaurus' AntiDetectDriver, and returns the fully rendered HTML. Designed for debugging visibility: the browser always runs headed inside an Xvfb virtual display, accessible live via NoVNC.

## API

### `POST /render`

Renders a URL and returns the full page HTML.

**Request body (JSON):**

```json
{
  "url": "https://example.com",
  "wait_for": "#main-content",
  "timeout": 30
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `url` | string | yes | — | URL to render |
| `wait_for` | string | no | — | CSS selector to wait for before returning HTML |
| `timeout` | integer | no | 30 | Seconds before timeout |

**Success response:** `200 text/html` — the full rendered `page_source`.

**Error responses:**

| Status | Error key | Condition |
|---|---|---|
| 400 | `invalid_url` | Missing or malformed URL |
| 502 | `navigation_failed` | Browser could not load the page |
| 503 | `pool_exhausted` | All workers busy, no capacity |
| 504 | `timeout` | Page load or `wait_for` selector timed out |

Error body: `{ "error": "<key>", "detail": "<message>" }`

### `GET /health`

Returns worker pool status.

```json
{ "status": "ok", "workers_busy": 1, "workers_total": 3 }
```

### `GET /vnc`

Returns an HTML page with an embedded NoVNC iframe pointing to port 6080. Provides a live view of the browser display for debugging scraping behavior.

## Architecture

### File Structure

```
botosaurus-api-service/
├── app/
│   ├── main.py          # FastAPI app, endpoint definitions
│   ├── browser_pool.py  # ThreadPoolExecutor + Driver lifecycle
│   └── renderer.py      # render(driver, url, wait_for, timeout) -> str
├── Dockerfile
├── docker-compose.yml
└── entrypoint.sh
```

### Components

**`app/browser_pool.py`**  
Manages N `AntiDetectDriver` instances. All drivers are initialized at startup (warm pool — no cold-start delay per request). Uses a `threading.Semaphore` and a `queue.Queue` to safely distribute drivers across concurrent requests.

**`app/renderer.py`**  
Single function: `render(driver, url, wait_for, timeout) -> str`.  
1. `driver.get(url)`
2. If `wait_for` is set: `WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, wait_for)))`
3. If no `wait_for`: waits for `document.readyState == "complete"` up to `timeout`
4. Returns `driver.page_source`

**`app/main.py`**  
FastAPI app. `POST /render` acquires a driver from the pool, calls `renderer.render()`, releases the driver, returns HTML. Maps exceptions to HTTP status codes.

### Display Stack

| Component | Role | Port |
|---|---|---|
| Xvfb `:99` | Virtual X11 display (1280×720) | — |
| x11vnc | Exposes display as VNC server | 5900 (internal) |
| noVNC + websockify | Web-based VNC client | 6080 |
| uvicorn | FastAPI server | 8000 |

All browser windows render on `:99` and are visible in real time via `http://localhost:6080/vnc.html`.

## Docker

### Base Image

`nikolaik/python-nodejs:python3.12-nodejs18-slim` — provides Python 3.12 and Node.js 18. Chrome, Xvfb, x11vnc, and noVNC are installed on top.

### Startup (`entrypoint.sh`)

```bash
#!/bin/bash
Xvfb :99 -screen 0 1280x720x24 &
x11vnc -display :99 -forever -nopw &
websockify --web /opt/novnc 6080 localhost:5900 &
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Exposed Ports

| Port | Service |
|---|---|
| 8000 | FastAPI Render API |
| 6080 | noVNC Browser View |

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MAX_WORKERS` | `3` | Number of parallel browser instances |
| `DISPLAY` | `:99` | X11 display for Chrome |

### `docker-compose.yml` excerpt

```yaml
services:
  botosaurus-api:
    build: .
    ports:
      - "8000:8000"
      - "6080:6080"
    environment:
      MAX_WORKERS: 3
      DISPLAY: ":99"
```

## Concurrency Model

- `MAX_WORKERS` browser instances run permanently (warm).
- A `threading.Semaphore(MAX_WORKERS)` gates access.
- If all workers are busy and a request arrives, it immediately returns `503` (no queuing — fail fast).
- Each worker owns its driver instance exclusively during a request; no sharing.

## Non-Goals

- Authentication / API key protection (out of scope; service is intended for internal/trusted use)
- Asynchronous job queue (sync-only by design)
- Proxy configuration per request (can be added later via driver options)
- Screenshot or PDF export
