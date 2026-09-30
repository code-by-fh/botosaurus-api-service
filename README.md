# page-render-service

A scraping API that returns the **fully rendered** HTML (or Markdown) of any URL. Many client
applications can use it at the same time. It is built to run on a small VPS (for example a
Hetzner CX33) and to look like a real desktop Chrome to the sites it visits.

- **Real Chrome via [zendriver](https://github.com/cdpdriver/zendriver)**: CDP-based, with no
  WebDriver traces. Each request runs in its own fresh browser context, so no cookies or storage
  leak between client apps. The one exception are allow-listed anti-bot clearance cookies, which
  are reused briefly per egress route (see [Clearance reuse](#completeness-guarantee)).
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
- **Optional live view** of the headed browsers via noVNC, served through the API port behind
  authentication.

## Quick start

```bash
cp .env.example .env            # then set API_KEYS
docker compose up -d --build
curl -X POST http://localhost:8000/api/v1/render \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

## API

Interactive documentation (`/docs`, `/redoc`, `/openapi.json`) is off by default. With
`ENABLE_DOCS=true` it is served behind HTTP Basic auth: any username, password = one of the API
keys. Without it these paths return `404`.

### `POST /api/v1/render` (Bearer token)

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Absolute http(s) URL, at most 2048 characters. |
| `mode` | `auto` \| `browser` | `auto` | `auto` uses plain HTTP only where it is verified to be complete. `browser` always uses Chrome. |
| `wait_for` | string | - | CSS selector that must exist before the page counts as rendered. Also required in HTTP results. |
| `selector` | string | - | CSS selector of the element to return (aliases: `element`, `target`). |
| `timeout` | int | 30 | Render budget in seconds (5-120). Time spent in the queue is not included. |
| `idle_timeout` | int | - | Requires `wait_for`. Give up early (`504`) when the loaded page shows no DOM or XHR/fetch activity for this many seconds and the element is still missing. |
| `wait_for_settle` | number | 3 | Requires `wait_for`; browser only. Seconds (0-10, fractions allowed) the element must stay present on the loaded page before a page whose content keeps changing is returned with `X-Render-Stable: false`. Smaller values return sooner but may cut off content that is still being appended after the element appeared. `0` returns as soon as the element is there on a loaded page without a challenge. A page that settles earlier is returned earlier anyway. |
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

Every response, including errors, also carries `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: SAMEORIGIN` and
`Content-Security-Policy: frame-ancestors 'self'`.

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
| 429 | `TOO_MANY_AUTH_FAILURES` | 10 wrong API keys from this client within 5 minutes; every request is refused until `Retry-After` |
| 502 | `NAVIGATION_FAILED` | DNS, connection or TLS failure, or a redirect to a forbidden address |
| 502 | `TARGET_BLOCKED` | An anti-bot challenge did not resolve within the timeout |
| 504 | `TIMEOUT` | `wait_for` never appeared, or the browser stopped responding |

### Other endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /health` | none | Liveness probe |
| `GET /health/detail` | Bearer | Pool utilisation, restarts, learned verdicts, number of stored clearance cookies |
| `GET /vnc` | Basic (password = API key) | noVNC live view page; sets the viewer session cookie. `404` unless `ENABLE_VNC=true` |
| `GET /vnc/app/{path}` | session cookie or Basic | noVNC files relayed from the container; `404` for unknown or disallowed paths, `502 VNC_UNAVAILABLE` if noVNC does not answer |
| `WS /vnc/websockify` | session cookie or Basic, same `Origin` | VNC stream relayed from the container |
| `GET /docs`, `/redoc`, `/openapi.json` | Basic (password = API key) | API documentation; `404` unless `ENABLE_DOCS=true` |

All authenticated endpoints share the failed-login lockout: after 10 wrong API keys (Bearer or
Basic) from one client address within 5 minutes, that address gets `429 TOO_MANY_AUTH_FAILURES`
with `Retry-After`, even for a correct key, until the window has passed. Requests without any
credentials are not counted. The client address is the TCP peer; `X-Forwarded-For` is only used
when the peer is listed in `TRUSTED_PROXY_IPS`.

### Live view (VNC)

With `ENABLE_VNC=true` and `VNC_PASSWORD` set (headed mode only), open `https://<your-domain>/vnc`:

1. The browser asks for credentials. Enter any username and an API key as password.
2. The page frames the noVNC viewer from the same origin (`/vnc/app/vnc.html`) and sets a
   short-lived session cookie (`vnc_session`: HttpOnly, SameSite=Strict, Path=/vnc, 8 h,
   `Secure` when the request arrived over HTTPS according to `X-Forwarded-Proto`). The viewer's
   files and its WebSocket authenticate with that cookie, because browsers do not reliably send
   Basic credentials on WebSocket upgrades.
3. noVNC asks for the `VNC_PASSWORD`.

The noVNC server listens on `127.0.0.1` inside the container only; no extra port is published.
The WebSocket is refused unless its `Origin` matches the `Host` (or `X-Forwarded-Host`) of the
request, so a reverse proxy must preserve `Host` or set `X-Forwarded-Host`, and must forward
WebSocket upgrades on `/vnc/websockify`. Session cookies are signed with a key generated at
startup, so a restart logs every viewer out.

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

**Anti-bot challenges.** A challenge page is never returned as content. In the browser the
service keeps waiting while a challenge is shown, because many challenges solve themselves and
reload the page. If it is still shown at `timeout`, the request fails with `502 TARGET_BLOCKED`.
On the HTTP fast path a challenge sends the request to the browser and makes the section
browser-only. A page counts as a challenge when:

- it is a known vendor block page (Cloudflare, DataDome, PerimeterX, Akamai, Imperva: typical
  titles such as "Just a moment..." or vendor markers in the markup), whatever its size; or
- it has little visible text (under 1000 characters) **and** either
  - a human-verification phrase in its title or an `h1`/`h2`, in German, English, French or
    Spanish ("Ich bin kein Roboter", "Are you human?", "Verify you are human",
    "Checking your browser", "Êtes-vous un humain ?", "No soy un robot", ...), or
  - the script or widget of a challenge SDK: AWS WAF (`awswaf.com` + `challenge.js`/`captcha.js`,
    `AwsWafIntegration`), hCaptcha, Cloudflare Turnstile, or a reCAPTCHA challenge frame or
    checkbox.

Only whole phrases count, so titles such as "Robot vacuum cleaners" or "Human Resources" do not.
**Clearance reuse.** Once a browser render passes a challenge, the vendor sets a clearance cookie
(for example `aws-waf-token` or `cf_clearance`). With `CLEARANCE_REUSE=true` (default) the
service keeps that cookie for up to `CLEARANCE_MAX_AGE_SECONDS` (default 240 s, never beyond the
cookie's own expiry) and sets it in the fresh browser context of the next render to the same
site on the same egress route, so the challenge and its reload are not paid again.

- Only an allow-list of vendor clearance cookies is kept: `aws-waf-token`, `cf_clearance`,
  `datadome`, `reese84`, `incap_ses_*`, `visid_incap_*`, `_px3`, `_pxvid`, `_abck`, `bm_sz`.
  Session, login, consent and tracking cookies are never carried over.
- Cookies are kept per egress route. A token earned via `HOME_PROXY` is never sent directly, and
  the other way round, because vendors bind it to the client IP.
- A cookie is stored only after a render that ended without a challenge and with a status below
  400. If a render that used a stored cookie still ends on a challenge, the cookie is dropped.
- The cookies are held in memory only, shared by all client apps of the service, and never sent
  over the HTTP fast path. `/health/detail` shows their number, never their values.

The size limit exists because these SDKs also run on normal pages (AWS WAF token acquisition, a
Turnstile or hCaptcha contact form, the reCAPTCHA v3 badge); a page with real content is never
treated as a challenge because of them. The rules are generic; there are no per-site rules.

If you know an element that only exists once the content you need is loaded, pass it as `wait_for`.
It is the strongest signal for both engines. Use `mode: "browser"` to skip HTTP entirely.

**How long `wait_for` waits:**

| Situation | Result |
|---|---|
| Element present, page settles | Returned as soon as the page is quiet for about one second |
| Element present, page never settles (carousels, tickers) | Returned `wait_for_settle` seconds (default 3) after the element appeared on the loaded page, `X-Render-Stable: false` |
| Element missing, `idle_timeout` set | `504` once the loaded page has been idle that long |
| Element missing, no `idle_timeout` | `504` when `timeout` is reached |

The early give-up is opt-in on purpose. An idle page does not prove the element will not come:
content scheduled by a timer or pushed over a websocket arrives without prior activity. On
`quotes.toscrape.com/js-delayed/` the content appears after about 10 s of complete silence, so a
default idle cut-off would have reported it as missing.

## Render timing log

Every `POST /api/v1/render` writes one `INFO` line on the `render.api` logger, also for
failures, so slow requests can be broken down:

```
Render timing traceId=4f1c... url=https://example.com/a outcome=ok engine=browser host_wait_ms=0 queue_ms=2 context_ms=41 navigate_ms=1830 readiness_ms=21950 read_ms=64 output_ms=35 total_ms=23930 readiness_end=load-budget-expired challenge_polls=0 clearance=none
```

| Key | Meaning |
|---|---|
| `traceId` | Same value as the `X-Request-ID` response header and the error body's `traceId`. |
| `outcome` | `ok`, or the error `code` of the response (`TIMEOUT`, `TARGET_BLOCKED`, ...). |
| `engine` | `http`, `browser`, or `none` when the request failed. |
| `host_wait_ms` | Waiting for a per-host slot (`MAX_CONCURRENCY_PER_HOST`). |
| `http_ms` | The HTTP fast path attempt; only present when one was made. |
| `queue_ms` | Waiting for a free browser worker (`MAX_WORKERS`, `MAX_QUEUE_SIZE`). |
| `restart_ms` | Restarting a due browser before the render; only present when it happened. |
| `context_ms` | Opening the fresh browser context (plus resource blocking, if requested, and setting and reading clearance cookies). |
| `navigate_ms` | Navigation until Chrome committed the main document. |
| `readiness_ms` | Waiting until the page counted as rendered (see [Completeness guarantee](#completeness-guarantee)). |
| `read_ms` | Reading the DOM, final URL and status from the page. |
| `output_ms` | Selector extraction and Markdown conversion. |
| `total_ms` | Whole request, including time outside the listed phases. |
| `readiness_end` | Why the readiness wait stopped: `settled` (quiet before 60% of `timeout`), `load-budget-expired` (quiet only once network activity was ignored), `wait-for-found` (element present, DOM never settled), `deadline` (still changing at `timeout`), `challenge`, `element-missing`, `idle-give-up`, or `interrupted` (the browser hung). |
| `challenge_polls` | Readiness polls that saw an anti-bot challenge, including one that later cleared. |
| `clearance` | Clearance cookie reuse (browser renders only, absent with `CLEARANCE_REUSE=false`): `reused` (stored cookies were set and not rejected), `stored` (none were set, the render earned new ones), `dropped` (stored cookies were set, but the challenge stayed, so they were forgotten), or `none`. |

Phases that did not run are left out. A high `readiness_ms` with `readiness_end=deadline` or
`load-budget-expired` points to a page that keeps changing or polling; pass `wait_for` to end
the wait as soon as the needed content exists. A high `queue_ms` means the pool is too small for
the load.

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
| `CLEARANCE_REUSE` | `true` | Reuse anti-bot clearance cookies between browser renders of the same site and egress route (see [Completeness guarantee](#completeness-guarantee)). |
| `CLEARANCE_MAX_AGE_SECONDS` | `240` | Longest time a stored clearance cookie is reused (1-3600); a shorter cookie expiry wins. Below the typical 300 s token immunity. |
| `ALLOW_PRIVATE_TARGETS` | `false` | Allow private, loopback and link-local targets. Local development only. |
| `ENABLE_VNC` | `false` | Start x11vnc and noVNC (headed mode only). |
| `VNC_PASSWORD` | - | Required when `ENABLE_VNC=true`. VNC uses at most 8 characters. |
| `VNC_PORT` | `6080` | Container-internal port of the noVNC server on `127.0.0.1`. Never published; `/vnc` relays it. |
| `ENABLE_DOCS` | `false` | Serve `/docs`, `/redoc` and `/openapi.json` behind HTTP Basic auth (password = API key). `false`: `404`. |
| `TRUSTED_PROXY_IPS` | - | Comma-separated IPs or CIDR ranges of reverse proxies whose `X-Forwarded-For` is trusted to identify clients for the failed-login lockout. Set it behind a proxy, otherwise all clients share the proxy's address. |
| `BROWSER_SANDBOX` | `false` | Chrome sandbox. It needs user namespaces, which containers usually lack. |
| `CHROME_BIN` | auto | Chrome executable. The image uses `/usr/local/bin/chrome-launcher`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Timestamps are UTC. |

## Deployment on a small VPS

- **Server:** a Hetzner CX33 (4 vCPU / 8 GB) with `MAX_WORKERS=3`. Raise it only after watching
  `/health/detail` and memory. The browser pool is CPU-bound before it is RAM-bound. The image is
  amd64 only, so do not use the ARM (CAX) plans.
- **Hardening:** `docker-compose.yml` runs the container read-only, as a non-root user, with all
  capabilities dropped and memory/CPU limits. Port 8000 is the only published port. Put TLS and
  IP allow-listing in a reverse proxy in front of it, and list that proxy in `TRUSTED_PROXY_IPS`.
- **Egress IP:** Hetzner addresses are well-known datacenter ranges. Strongly protected sites
  (Cloudflare Bot Management, DataDome, Akamai, ...) challenge them whatever the browser
  fingerprint. For those, set `HOME_PROXY` (for example a proxy at home reached over
  WireGuard/Tailscale, or an ISP/residential proxy) and send `use_proxy: true`. Make
  `BROWSER_LOCALE` and `BROWSER_TIMEZONE` match that exit.
- **Debug view:** set `ENABLE_VNC=true` and `VNC_PASSWORD`, then visit `https://<your-domain>/vnc`
  (see [Live view (VNC)](#live-view-vnc)).

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
- Verdicts and clearance cookies are held in memory and are relearned after a restart.
- The failed-login lockout and the VNC session key live in the memory of one process. Running
  several uvicorn workers would split the lockout counters and invalidate cookies across
  workers; the image runs one.
- The service speaks plain HTTP. Without a TLS-terminating proxy in front, API keys and the VNC
  session cookie travel in clear text.

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
  auth.py            API-key checks (Bearer, Basic); auth_throttle.py: failed-login lockout
  client_address.py  client address, honouring X-Forwarded-For only from trusted proxies
  docs_routes.py     guarded Swagger UI, ReDoc and OpenAPI schema
  security_headers.py security headers on every response
  vnc.py             /vnc routes; vnc_proxy.py relays noVNC; vnc_session.py signs the cookie
  api_models.py      request model and validation
  runtime.py         wiring of all components
  timing.py          per-request phase durations for the render timing log
  scraping/          engine choice (scraper), verification, verdicts, per-host limits,
                     clearance cookie store
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
