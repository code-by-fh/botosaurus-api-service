# botosaurus-api-service

A Docker-based HTTP service that renders URLs in a real Chromium browser via [Botosaurus'](https://github.com/omkarcloud/botosaurus) `AntiDetectDriver` and returns the fully rendered HTML. Built for scraping JavaScript-heavy pages (SPAs, React apps) where raw HTTP requests won't cut it.

## Features

- **Real browser rendering** — Chromium via Botosaurus AntiDetectDriver, not a headless HTTP client
- **Warm browser pool** — N browser instances ready at startup, no cold-start delay per request
- **Live debug view** — browser runs in a virtual display, accessible in real time via NoVNC at `/vnc`
- **Simple HTTP API** — send a URL, get back rendered HTML

## Quick Start

```bash
docker run -p 8000:8000 -p 6080:6080 doublelayer/botosaurus-api-service:latest
```

Or with Docker Compose:

```yaml
services:
  botosaurus-api:
    image: doublelayer/botosaurus-api-service:latest
    ports:
      - "8000:8000"   # Render API
      - "6080:6080"   # NoVNC debug view
    environment:
      MAX_WORKERS: 3
```

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

**Success:** `200 text/html` — the fully rendered page source.

**Errors:**

| Status | Key | Condition |
|---|---|---|
| 400 | `invalid_url` | Missing or malformed URL |
| 502 | `navigation_failed` | Browser could not load the page |
| 503 | `pool_exhausted` | All workers busy |
| 504 | `timeout` | Page load or selector timed out |

Error body: `{ "error": "<key>", "detail": "<message>" }`

**Example (Python):**

```python
import requests

response = requests.post("http://localhost:8000/render", json={
    "url": "https://example.com",
    "wait_for": "#main-content",
})
html = response.text
```

### `GET /health`

Returns worker pool status.

```json
{ "status": "ok", "workers_busy": 1, "workers_total": 3 }
```

### `GET /vnc`

Returns an HTML page with an embedded NoVNC view — watch the browser live at `http://localhost:6080/vnc.html`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MAX_WORKERS` | `3` | Number of parallel browser instances |
| `HEADLESS` | `true` | Set to `false` to enable headed mode with NoVNC |

## Design

See [`docs/superpowers/specs/2026-06-04-botosaurus-render-service-design.md`](docs/superpowers/specs/2026-06-04-botosaurus-render-service-design.md) for the full architecture and design decisions.
