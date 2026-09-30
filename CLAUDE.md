# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI scraping API (Python 3.12). `POST /api/v1/render` returns the fully rendered HTML or
Markdown of a URL. It serves many client apps from one small VPS (Hetzner CX-class), renders in
real Google Chrome through zendriver, and must be as hard as possible to detect as a bot. The
central promise to callers: **never return a half-rendered page**. README.md documents the API,
all environment variables and deployment; `docs/adr/` holds the architecture decisions.

## Commands

```bash
pip install -r requirements-dev.txt

pytest                                                  # full suite; needs no Chrome, no network
pytest tests/test_scraper.py::test_browser_mode_never_uses_http   # single test
ruff check app tests && ruff format --check app tests   # what CI runs

# Local server against an installed Chrome (there is no module-level `app`, use --factory):
API_KEYS=dev HEADLESS=true ALLOW_PRIVATE_TARGETS=true \
  uvicorn --factory app.main:create_app --port 8000

docker compose up --build -d   # full stack incl. Xvfb and optional noVNC (ENABLE_VNC=true + VNC_PASSWORD)
                               # live view at /vnc, docs at /docs with ENABLE_DOCS=true (both Basic auth)
```

- Run ruff on `app tests` only. `ruff format .` also rewrites the code blocks inside README and
  `docs/`.
- After changing `.env`, run `docker compose up -d`, not `restart`. Only `up -d` recreates the
  container with the new environment. `docker-compose.yml` uses `${VAR:-default}` so that `.env`
  wins; never hard-code values in its `environment:` block, because they would silently override
  `.env`.

## Request flow

`app/runtime.py` wires everything; `app/main.py` only maps HTTP to `Scraper.scrape()`.

1. `UrlGuard.check` rejects non-http(s) URLs and hosts that resolve to non-public addresses. It
   also checks that a proxy is configured when `use_proxy` is set.
2. `HostLimiter` caps concurrent requests per target host.
3. **HTTP fast path** (`mode: "auto"` only). curl_cffi is used only when the `VerdictStore` says
   `HTTP_SUFFICIENT` for the section (`host/first-path-segment/depth`) and the response passes
   `find_incompleteness`. If either fails, the request falls back to the browser.
4. **Browser**. `BrowserPool` (bounded queue, `429` when full) hands out a `BrowserWorker`, which
   recycles Chrome after N pages or M minutes or when it is unhealthy. `page_loader` opens a fresh
   browser context per request. Before navigating it sets stored anti-bot clearance cookies for
   the host and egress route (`browser/clearance.py`, store in `scraping/clearance.py`, ADR 0003);
   after a render without challenge and status < 400 it harvests them. `readiness.ReadinessWaiter`
   decides when the page is rendered.
5. After a stable browser render with status 200 of an `UNKNOWN` section, `HttpVerifier` fetches
   the same URL over HTTP in the background and compares the visible text
   (`content/text.compare_texts`). That comparison is the only way a section can become
   `HTTP_SUFFICIENT`.

**Egress**: Chrome (browser-wide `--proxy-server` and every context) and curl_cffi never connect
to targets directly. They go through `app/egress.py`, a local forward proxy that vets every
connection with the `UrlGuard` and connects only to the vetted address. `HOME_PROXY` is chained
behind it. The `<-loopback>` bypass rule in `browser/session.py` is essential: without it, Chrome
sends loopback and link-local traffic (including the cloud metadata service `169.254.169.254`)
around any proxy.

**Access surfaces** (ADR 0002): port 8000 is the only published port. `app/auth.py`
`Authenticator` checks every Bearer/Basic key and feeds the failed-login lockout
(`auth_throttle.py`, keyed by `client_address.py`). `/docs`, `/redoc`, `/openapi.json` exist only
with `ENABLE_DOCS=true` (`docs_routes.py`, Basic auth). The live view exists only with
`ENABLE_VNC=true`: `GET /vnc` (Basic) sets a signed session cookie (`vnc_session.py`); `/vnc/app/...`
and `WS /vnc/websockify` accept that cookie or Basic and are relayed to websockify on
`127.0.0.1:VNC_PORT` (`vnc_proxy.py`).

## Rules that are easy to break

- **Completeness is conservative by design.** Any change to `content/completeness.py`,
  `content/text.py`, `scraping/verdicts.py`, `scraping/verifier.py` or `browser/readiness.py`
  must keep the rule "when in doubt, render in the browser".
  - One mismatch marks a section browser-only.
  - Matches count distinct paths.
  - Every number the browser showed must be present in the HTTP document.
  - Readiness ignores network activity after the load budget, because pages that poll would
    otherwise never settle.
  - Challenge definitions live only in `content/completeness.py`; the readiness probe receives
    them as JSON (`CHALLENGE_SIGNALS`), so patterns must be valid in Python and JavaScript. No
    per-site rules. Generic signals (verification phrases, challenge SDKs) count only on pages
    under `INTERSTITIAL_MAX_TEXT_CHARS`, because a false positive in the browser waits for the
    full timeout and then fails with `TARGET_BLOCKED`.
  - `idle_timeout` (early give-up when a `wait_for` element is missing) is opt-in. A quiet page
    does not prove nothing is coming: `quotes.toscrape.com/js-delayed/` is silent for about 10 s
    before its content appears.
- **Every Chrome switch is a fingerprint.** Keep `browser_args()` minimal.
  - Chrome on Linux ignores `--lang`. Language and timezone come from `LANGUAGE`/`TZ`, which
    `docker/chrome-launcher.sh` sets for Chrome only, so the service itself stays in UTC.
  - Resource blocking is opt-in per request, because blocking is itself a bot signal.
  - A known unfixable tell: WebGL reports SwiftShader, because the VPS has no GPU.
- **zendriver is pinned (0.17.0) and partly used through internals**: `zendriver.core.proxy`
  (`ProxyError`, `UpstreamProxy`, `pipe`, `copy_stream`), `ProtocolException`,
  `Browser._process`, `Browser.config`. Re-check these on every upgrade.
- **Errors**: raise a `ServiceError` subclass from `app/errors.py`. Its handlers produce the
  envelope `{"error": {"code", "message", "traceId"}}`; validation errors return `400` with
  `fields`, not FastAPI's 422. New response headers or error cases must also go into
  `app/openapi_docs.py`; `tests/test_api.py` checks the documented headers.
- **Access surfaces stay authenticated.** No route may relay noVNC or serve docs without the
  `Authenticator`. Never publish websockify's port or bind it beyond `127.0.0.1`. The VNC
  WebSocket must keep its same-`Origin` check. Do not add `default-src` to the CSP in
  `security_headers.py`: Swagger UI loads its assets from a CDN.
- **Clearance reuse is the only state shared between browser contexts.** Only names in
  `CLEARANCE_ALLOW_LIST` are stored, keyed by egress route (a token is bound to the egress IP),
  capped by `CLEARANCE_MAX_AGE_SECONDS`, never stored after an unresolved challenge, dropped when
  an injected token still ends on a challenge, never sent over curl_cffi, never logged. Inject
  and harvest failures must not fail the render.
- **Plain-HTTP egress relays exactly one request per client connection** and forces
  `Connection: close`. Reusing the connection would let a request for another host bypass the
  guard.

## Tests

- Fakes live at the system boundaries in `tests/fakes.py`:
  - `FakeBrowser`/`FakeTab` answer zendriver's CDP command generators like Chrome would.
  - `FakeHttpFetcher` replaces curl_cffi.
  - `public_resolver` replaces DNS.
- Inject them via `runtime.Adapters` (`launcher`, `fetcher_factory`, `resolver`, `pause`).
  Everything in between runs for real.
- `test_http_fetcher.py` and `test_egress.py` run the real fetcher and egress proxy against local
  HTTP servers.
- Async tests use `@pytest.mark.anyio`. `conftest.py` sets the readiness poll interval to 0. Time
  windows are injected (`ReadinessTimings`, clocks), so tests do not sleep.

## Documentation to keep in sync

Update these in the same change:
- README.md: the configuration table (variable, default, purpose) and the API tables.
- CHANGELOG.md: SemVer, with breaking changes listed first.
- An ADR in `docs/adr/` for architectural decisions.
