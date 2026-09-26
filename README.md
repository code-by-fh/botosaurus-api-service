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
    env_file:
      - .env          # Must contain API_KEY=<your-key>
    environment:
      MAX_WORKERS: 3
```

## Authentication

All endpoints except `/health` require a valid API key.

### Setup

1. Generate a strong random key:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

2. Create a `.env` file (see `.env.example`):
   ```
   API_KEY=<your-generated-key>
   ```

3. The `.env` file is already in `.gitignore` — **never commit your key**.

### API Endpoints (`/render`) — Bearer Token

Programmatic endpoints use the `Authorization: Bearer <key>` header:

```bash
curl -X POST http://localhost:8000/render \
  -H "Authorization: Bearer <your-key>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

### Browser Endpoints (`/vnc`) — HTTP Basic Auth

The `/vnc` debug view uses HTTP Basic Auth so your browser shows a native login dialog. Enter any username and the API key as password.

**Without valid credentials you'll receive `401 Unauthorized`.**


## API

### `POST /render`

Renders a URL and returns the full page content as HTML or Markdown.

**Request body (JSON):**

```json
{
  "url": "https://example.com",
  "wait_for": "#main-content",
  "timeout": 30,
  "format": "html"
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `url` | string | yes | — | URL to render |
| `wait_for` | string | no | — | CSS selector to wait for before returning |
| `timeout` | integer | no | 30 | Seconds before timeout |
| `format` | string | no | `"html"` | Output format: `"html"` or `"markdown"` |

**Success:**
- `format: "html"` → `200 text/html` — the fully rendered page source.
- `format: "markdown"` → `200 text/markdown` — the page converted to Markdown (via [markdownify](https://github.com/matthewwithanm/python-markdownify)).

**Errors:**

| Status | Key | Condition |
|---|---|---|
| 400 | `invalid_url` | Missing or malformed URL |
| 422 | validation error | Invalid `format` value |
| 502 | `navigation_failed` | Browser could not load the page |
| 503 | `pool_exhausted` | All workers busy |
| 504 | `timeout` | Page load or selector timed out |

Error body: `{ "error": "<key>", "detail": "<message>" }`

**Examples (Python):**

```python
import requests

headers = {"Authorization": "Bearer <your-key>"}

# Get rendered HTML
html = requests.post("http://localhost:8000/render",
    headers=headers,
    json={"url": "https://example.com", "wait_for": "#main-content"},
).text

# Get rendered Markdown
markdown = requests.post("http://localhost:8000/render",
    headers=headers,
    json={"url": "https://example.com", "format": "markdown"},
).text
```

### `GET /health`

Public liveness probe — no authentication required.

```json
{ "status": "ok" }
```

### `GET /health/detail` 🔒

Detailed worker pool status. Requires Bearer token.

```json
{ "status": "ok", "workers_busy": 1, "workers_total": 3 }
```

### `GET /vnc` 🔒

Returns an HTML page with an embedded NoVNC view. Protected by HTTP Basic Auth (password = API key).

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | — (**required**) | Bearer token for API authentication |
| `MAX_WORKERS` | `3` | Number of parallel browser instances |
| `HEADLESS` | `true` | Set to `false` to enable headed mode with NoVNC |
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Design

See [`docs/superpowers/specs/2026-06-04-botosaurus-render-service-design.md`](docs/superpowers/specs/2026-06-04-botosaurus-render-service-design.md) for the full architecture and design decisions.
