# botosaurus-api-service

A scraping API that returns the **fully rendered** HTML (or Markdown) of any URL. Many client
applications can use it at the same time. It is built to run on a small VPS (for example a
Hetzner CX33) and to look like a real desktop Chrome to the sites it visits.

- **Real Chrome via [zendriver](https://github.com/cdpdriver/zendriver)**: CDP-based, with no
  WebDriver traces. Each request runs in its own fresh browser context, so no cookies or storage
  leak between client apps.
- **Verified HTTP fast path**: sections of a site that are proven to deliver the complete page
  without JavaScript are fetched with [curl_cffi](https://github.com/lexiforest/curl_cffi), which
  presents Chrome's TLS/HTTP2 fingerprint. That takes about 0.2 s instead of 2 s or more. All other
  pages are rendered in the browser (see [Completeness guarantee](#completeness-guarantee)).
- **Robust on small machines**: a bounded wait queue instead of instant failures, per-host
  concurrency limits, browsers recycled after N pages or M minutes, and a watchdog for hung
  browsers.
- **SSRF protection for all traffic**: Chrome and the HTTP client reach the internet only through
  a local egress proxy. It checks every connection (including redirects, sub-resources and
  JavaScript navigations) and connects only to the public address it vetted.
- **Optional live view** of the headed browsers via noVNC.

## Quick start

```bash
cp .env.example .env            # then set API_KEYS
docker compose up -d --build
curl -X POST http://localhost:8000/api/v1/render \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

## API

Interactive documentation is served at `/docs` (OpenAPI).

### `POST /api/v1/render` (Bearer token)

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Absolute http(s) URL, at most 2048 characters. |
| `mode` | `auto` \| `browser` | `auto` | `auto` uses plain HTTP only where it is verified to be complete. `browser` always uses Chrome. |
| `wait_for` | string | - | CSS selector that must exist before the page counts as rendered. Also required in HTTP results. |
| `selector` | string | - | CSS selector of the element to return (aliases: `element`, `target`). |
| `timeout` | int | 30 | Render budget in seconds (5-120). Time spent in the queue is not included. |
| `idle_timeout` | int | - | Requires `wait_for`. Give up early (`504`) when the loaded page shows no DOM or XHR/fetch activity for this many seconds and the element is still missing. |
| `format` | `html` \| `markdown` | `html` | Output format. |
| `use_proxy` | bool | false | Route this request through `HOME_PROXY`. |
| `block_resources` | bool | false | Skip images, fonts, media and CSS in the browser. Faster, but detectable and may break pages. |

Unknown fields are rejected.

**Success:** `200` with the content as `text/html` or `text/markdown`, plus these headers:

| Header | Meaning |
|---|---|
| `X-Render-Engine` | `http` or `browser` |
| `X-Render-Stable` | `false` if the content was still changing when the timeout was reached |
| `X-Final-Url` | URL after redirects |
| `X-Upstream-Status` | HTTP status the target returned for the main document (`0` if unknown). A 404 page is still returned as content. |
| `X-Request-ID` | Trace id. Pass your own `X-Request-ID` to correlate logs. |

**Errors** use one envelope:
`{"error": {"code": "...", "message": "...", "traceId": "..."}}`. Validation errors also carry
`fields: [{"field", "message"}]`.

| Status | Code | Condition |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Invalid body (per-field details) |
| 400 | `TARGET_NOT_ALLOWED` | Non-http(s) URL, or a host resolving to a private or reserved address |
| 400 | `PROXY_NOT_CONFIGURED` | `use_proxy` without `HOME_PROXY` |
| 401 | `UNAUTHORIZED` | Missing or unknown API key |
| 404 | `ELEMENT_NOT_FOUND` | `selector` matched nothing |
| 429 | `SERVICE_BUSY` | Queue full or no capacity in time; honour `Retry-After` |
| 502 | `NAVIGATION_FAILED` | DNS, connection or TLS failure, or a redirect to a forbidden address |
| 502 | `TARGET_BLOCKED` | An anti-bot challenge did not resolve within the timeout |
| 504 | `TIMEOUT` | `wait_for` never appeared, or the browser stopped responding |

### Other endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /health` | none | Liveness probe |
| `GET /health/detail` | Bearer | Pool utilisation, restarts, learned verdicts |
| `GET /vnc` | Basic (password = API key) | noVNC live view; `404` unless `ENABLE_VNC=true` |

## Completeness guarantee

With `mode: "auto"`, a response is served over plain HTTP **only if both** of these hold:

1. **The site section is verified.** A section is a host, its first path segment and the path
   depth. For example, `/products` and `/products/42` are different sections.
   - After a browser render of an unverified section, the same URL is fetched once over HTTP,
     after a random 1-3 s pause.
   - The visible texts of both documents are compared.
   - The section is trusted only after `VERDICT_MIN_SAMPLES` **different URLs** matched. A URL
     matches when the HTTP document contains at least `MIN_TEXT_COVERAGE` (default 90%) of the
     browser's words and text length, and **every number** the browser showed (prices, stock
     levels and counts are typical client-rendered content).
   - A single mismatch marks the section browser-only.
   - Verdicts expire after `VERDICT_TTL_SECONDS`.
   - An HTTP response that redirects out of a verified section is not used.
2. **This very response passes every check.** The checks are: status 200 and HTML; no anti-bot
   challenge; no empty SPA mount point (`#root`, `#app`, `#__next`, ...); no "enable JavaScript"
   notice or meta refresh; enough visible text; and every `wait_for`/`selector` element present.
   If any check fails, the request is rendered in the browser and the section becomes
   browser-only.

In the browser, a page counts as rendered when four conditions hold at the same time:
- `document.readyState` is `complete`.
- No challenge is shown.
- The `wait_for` element exists.
- Visible text length and DOM node count have not changed for about one second. Until 60% of
  the timeout has passed, the number of finished XHR/fetch requests must not change either.
  After that, pages that keep polling or sending beacons still settle on their DOM.

Only renders with status 200 that became stable are used as a reference for verification.

If you know an element that only exists once the content you need is loaded, pass it as `wait_for`.
It is the strongest signal for both engines. Use `mode: "browser"` to skip HTTP entirely.

**How long `wait_for` waits:**

| Situation | Result |
|---|---|
| Element present, page settles | Returned as soon as the page is quiet for about one second |
| Element present, page never settles (carousels, tickers) | Returned 3 s after the element appeared, `X-Render-Stable: false` |
| Element missing, `idle_timeout` set | `504` once the loaded page has been idle that long |
| Element missing, no `idle_timeout` | `504` when `timeout` is reached |

The early give-up is opt-in on purpose. An idle page does not prove the element will not come:
content scheduled by a timer or pushed over a websocket arrives without prior activity. On
`quotes.toscrape.com/js-delayed/` the content appears after about 10 s of complete silence, so a
default idle cut-off would have reported it as missing.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `API_KEYS` | required | Comma-separated API keys, one per client app. `API_KEY` (single key) is still accepted. |
| `MAX_WORKERS` | `2` | Browser instances. About 2 on 4 GB / 2 vCPU, 3-4 on 8 GB / 4 vCPU. |
| `HEADLESS` | `false` | `false`: headed Chrome on Xvfb (harder to detect). `true`: `--headless=new`. |
| `HOME_PROXY` / `PROXY` | - | Upstream proxy for `use_proxy` requests. `http://`, `https://` or `socks5://`, credentials allowed; Chrome never sees them. |
| `BROWSER_LOCALE` | `de-DE` | Browser language and `Accept-Language`. Must match the egress IP. |
| `BROWSER_TIMEZONE` | `Europe/Berlin` | Browser timezone. Must match the egress IP. |
| `BROWSER_MAX_PAGES` | `150` | Restart a browser after this many renders. |
| `BROWSER_MAX_AGE_SECONDS` | `2700` | Restart a browser after this age. |
| `QUEUE_TIMEOUT_SECONDS` | `20` | How long a request waits for a free browser or host slot. |
| `MAX_QUEUE_SIZE` | `8` | Requests allowed to wait; beyond that `429` is returned at once. |
| `MAX_CONCURRENCY_PER_HOST` | `2` | Simultaneous requests per target host. |
| `HTTP_FIRST_ENABLED` | `true` | Enable the verified HTTP fast path. |
| `VERDICT_MIN_SAMPLES` | `2` | Distinct matching URLs needed before a section is served over HTTP. |
| `VERDICT_TTL_SECONDS` | `21600` | Lifetime of a learned verdict. |
| `MIN_TEXT_COVERAGE` | `0.9` | Share of the browser's words and text length the HTTP document must contain (0-1). |
| `MAX_RESPONSE_BYTES` | `10485760` | Size cap for HTTP responses. Larger responses go to the browser. |
| `ALLOW_PRIVATE_TARGETS` | `false` | Allow private, loopback and link-local targets. Local development only. |
| `ENABLE_VNC` | `false` | Start x11vnc and noVNC (headed mode only). |
| `VNC_PASSWORD` | - | Required when `ENABLE_VNC=true`. VNC uses at most 8 characters. |
| `VNC_PORT` / `NOVNC_PREFIX` | `6080` / - | Where `/vnc` points the viewer. The prefix is for use behind a reverse proxy. |
| `BROWSER_SANDBOX` | `false` | Chrome sandbox. It needs user namespaces, which containers usually lack. |
| `CHROME_BIN` | auto | Chrome executable. The image uses `/usr/local/bin/chrome-launcher`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Timestamps are UTC. |

## Deployment on a small VPS

- **Server:** a Hetzner CX33 (4 vCPU / 8 GB) with `MAX_WORKERS=3`. Raise it only after watching
  `/health/detail` and memory. The browser pool is CPU-bound before it is RAM-bound. The image is
  amd64 only, so do not use the ARM (CAX) plans.
- **Hardening:** `docker-compose.yml` runs the container read-only, as a non-root user, with all
  capabilities dropped and memory/CPU limits. Put TLS and IP allow-listing in a reverse proxy in
  front of port 8000.
- **Egress IP:** Hetzner addresses are well-known datacenter ranges. Strongly protected sites
  (Cloudflare Bot Management, DataDome, Akamai, ...) challenge them whatever the browser
  fingerprint. For those, set `HOME_PROXY` (for example a proxy at home reached over
  WireGuard/Tailscale, or an ISP/residential proxy) and send `use_proxy: true`. Make
  `BROWSER_LOCALE` and `BROWSER_TIMEZONE` match that exit.
- **Debug view:** set `ENABLE_VNC=true` and `VNC_PASSWORD`, then open an SSH tunnel to port 6080
  (published on localhost only) and visit `/vnc`.

### Known limits

- A VPS has no GPU, so WebGL reports a software renderer (SwiftShader). Bot-detection pages can
  see this. It cannot be fixed without a GPU or a patched browser build.
- Content that loads only after scrolling or clicking is not triggered.
- With `use_proxy`, the egress guard checks the host name on the VPS. The upstream proxy then
  resolves it again, so a name that points to a private address only from the proxy's network is
  not caught.
- The HTTP fast path re-fetches a URL without cookies shortly after a browser visit. That pattern
  and the curl_cffi Chrome version (which may differ from the installed Chrome) are observable
  by the target. Set `HTTP_FIRST_ENABLED=false` for the most sensitive targets.
- Chrome runs without its sandbox (`BROWSER_SANDBOX=false`), because containers usually lack the
  user namespaces it needs. The container hardening is the isolation boundary.
- Verdicts are held in memory and are relearned after a restart.

## Local development

```bash
pip install -r requirements-dev.txt
ruff check app tests && ruff format --check app tests
pytest
```

Tests use fakes at the Chrome (CDP), HTTP and DNS boundaries and need no network. To run the
service locally against an installed Chrome:

```bash
API_KEYS=dev HEADLESS=true ALLOW_PRIVATE_TARGETS=true \
  uvicorn --factory app.main:create_app --port 8000
```

## Architecture

```
app/
  main.py            routes, lifecycle, response mapping
  api_models.py      request model and validation
  runtime.py         wiring of all components
  scraping/          engine choice (scraper), verification, verdicts, per-host limits
  browser/           zendriver session, page loading, readiness, workers, pool
  fetch/             curl_cffi HTTP fetcher
  content/           completeness checks, text comparison, HTML/Markdown output
  egress.py          local egress proxies that enforce the SSRF guard on all traffic
  url_guard.py       SSRF rules and address vetting
  errors.py          error types and envelope
docker/              entrypoint and Chrome launcher (timezone/language)
docs/adr/            architecture decisions
```

See [CHANGELOG.md](CHANGELOG.md) for changes and the migration from 1.x.
