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
ruff check app tests scripts && ruff format --check app tests scripts   # what CI runs

# Local server against an installed Chrome (there is no module-level `app`, use --factory):
API_KEYS=dev-key-at-least-32-characters-long HEADLESS=true ALLOW_PRIVATE_TARGETS=true \
  uvicorn --factory app.main:create_app --port 8000

docker compose up --build -d   # full stack incl. Xvfb and optional noVNC (ENABLE_VNC=true + VNC_PASSWORD)
                               # live view at /vnc, docs at /docs with ENABLE_DOCS=true (both Basic auth)
```

- Run ruff on `app tests scripts` only. `ruff format .` also rewrites the code blocks inside
  README and `docs/`.
- After changing `.env`, run `docker compose up -d`, not `restart`. Only `up -d` recreates the
  container with the new environment. `docker-compose.yml` uses `${VAR:-default}` so that `.env`
  wins; never hard-code values in its `environment:` block, because they would silently override
  `.env`.

## Request flow

`app/runtime.py` wires everything; `app/main.py` only maps HTTP to `Scraper.scrape()`. Before
that, `BodyLimitMiddleware` refuses bodies over 64 KiB (`413`) and the `Authenticator` checks the
key.

1. `Scraper.scrape` refuses `use_proxy` without `HOME_PROXY`; `UrlGuard.check` rejects
   non-http(s) URLs and hosts that resolve to non-public addresses.
2. `HostLimiter` caps concurrent requests per target host.
3. **HTTP fast path** (`mode: "auto"` only). curl_cffi is used only when the `VerdictStore` says
   `HTTP_SUFFICIENT` for the section (`host/first-path-segment/depth`) and the response passes
   `find_incompleteness`. If either fails, the request falls back to the browser.
4. **Browser**. `BrowserPool` (bounded queue, `429` when full) hands out a `BrowserWorker`, which
   recycles Chrome after N pages or M minutes or when it is unhealthy. `page_loader` opens a fresh
   browser context per request. Before navigating it sets stored anti-bot clearance cookies for
   the host and egress route (`browser/clearance.py`, store in `scraping/clearance.py`, ADR 0003);
   after a render without challenge and status < 400 it harvests them. `readiness.ReadinessWaiter`
   decides when the page is rendered, fed by an `InflightTracker` attached before navigation.
5. In `mode: "auto"` with `HTTP_FIRST_ENABLED`: after a stable status-200 browser render without
   `block_resources` of an `UNKNOWN` section has passed its late-content watch (step 6) without
   growth, `HttpVerifier` fetches the same URL over HTTP in the background and compares the
   visible text (`content/text.compare_texts`). That comparison is the only way a section can
   become `HTTP_SUFFICIENT`.
6. **Section profiles** (`scraping/profiles.py`, ADR 0006). Before the browser render, the
   `SectionProfileStore` turns recent content timing of the section into `ReadinessHints`
   (`min_ready_seconds`, `quiet_floor_seconds`). After a usable render, `scraping/learning.py`
   records its timing and may have `BrowserPool` keep the tab open to watch for late content
   (`browser/late_content.py`) while no request waits; late growth raises the profile and
   records a verdict mismatch, a clean watch starts the verification of step 5. `UNKNOWN`
   sections are watched on every render.

**Egress**: Chrome (browser-wide `--proxy-server` and every context) and curl_cffi never connect
to targets directly. They go through `app/egress.py`, a local forward proxy that vets every
connection with the `UrlGuard` and connects only to the vetted address. `HOME_PROXY` is chained
behind it and is given the vetted IP, never the host name (no re-resolution into the home LAN).
The `<-loopback>` bypass rule in `browser/session.py` is essential: without it, Chrome sends
loopback and link-local traffic (including the cloud metadata service `169.254.169.254`) around
any proxy.

**Access surfaces** (ADR 0002): port 8000 is the only published port. `app/auth.py`
`Authenticator` checks every Bearer/Basic key and feeds the failed-login lockout
(`auth_throttle.py`, keyed by `client_address.py`). `/docs`, `/redoc`, `/openapi.json` exist only
with `ENABLE_DOCS=true` (`docs_routes.py`, Basic auth). The live view exists only with
`ENABLE_VNC=true`: `GET /vnc` (Basic) sets a signed session cookie (`vnc_session.py`);
`/vnc/app/...` and `WS /vnc/websockify` accept that cookie or Basic and are relayed to websockify on
`127.0.0.1:VNC_PORT` (`vnc_proxy.py`).

## Rules that are easy to break

- **Completeness is conservative by design.** Any change to `content/completeness.py`,
  `content/text.py`, `scraping/verdicts.py`, `scraping/verifier.py`, `scraping/learning.py`,
  `browser/readiness.py` or `browser/network_activity.py` must keep the rule "when in doubt,
  render in the browser".
  - One mismatch marks a section browser-only.
  - Matches count distinct pages: normalised paths (case, `;` parameters, slashes, escapes),
    and identical browser text counts once. One page counts again only after
    `VERDICT_SAME_PAGE_INTERVAL_SECONDS`.
  - Every number the browser showed (separators kept, `1.299,99` is one number) must be present
    in the HTTP document at least as often.
  - A render is verified only after a completed late-content watch saw no growth
    (`scraping/learning.py`). A skipped, stopped, failed or disabled watch verifies nothing.
  - Readiness (ADR 0005) is event-based: no content request in flight (`network_activity`,
    CDP network events; trackers, beacons, WebSockets, long polls excluded) and no new visible
    text (MutationObserver in an isolated world) for an adaptive quiet window, plus loaded
    document, no challenge, no visible loading placeholder, `wait_for` present. Network
    exclusions are safe only because DOM growth still holds the page back; placeholder rules may
    only lengthen waits.
  - Readiness ignores network activity after the load budget (emergency brake), because pages
    that poll would otherwise never settle. DOM growth still counts.
  - Never send `Runtime.enable` (CDP detection signal); evaluate via `browser/evaluation.py`
    (`Runtime.evaluate` without the `userGesture: true` that zendriver's `Tab.evaluate` sends).
    The observer lives in an isolated world so the page cannot see it.
  - Challenge definitions live only in `content/completeness.py`; the readiness probe receives
    them as JSON (`CHALLENGE_SIGNALS`), so patterns must be valid in Python and JavaScript. No
    per-site rules. Generic signals (verification phrases, challenge SDKs) count only on pages
    under `INTERSTITIAL_MAX_TEXT_CHARS`, because a false positive in the browser waits for the
    full timeout and then fails with `TARGET_BLOCKED`.
  - There is no early give-up when a `wait_for` element is missing; it is awaited until
    `timeout`. A quiet page does not prove nothing is coming: `quotes.toscrape.com/js-delayed/`
    is silent for about 10 s before its content appears.
  - The render API carries no timing knobs; the service decides. Do not add per-request
    readiness fields. Internal windows are named constants in `readiness.py`
    (e.g. `WAIT_FOR_QUIET_CAP_SECONDS`), read at call time so tests can shorten them.
- **Learned profiles may only lengthen waits.** `ReadinessHints` are floors; only observations
  of the current render may end a wait. Learn only from renders that pass
  `learning.is_usable_reference`. A late-content watch holds its worker, so it must never start
  while requests are queued and must stop when one arrives (a stop event, not task
  cancellation: a task cancelled before its first step skips its `finally`).
- **Every Chrome switch is a fingerprint.** Keep `browser_args()` minimal.
  - Chrome on Linux ignores `--lang`. Language and timezone come from `LANGUAGE`/`TZ`, which
    `docker/chrome-launcher.sh` sets for Chrome only, so the service itself stays in UTC.
  - Resource blocking is opt-in per request, because blocking is itself a bot signal
    (`app/browser/blocking.py`, ADR 0004). Send patterns only as
    `set_blocked_ur_ls(url_patterns=[BlockPattern(...)])` in URLPattern syntax, anchored to the
    path end (`*://*:*/*.css?*`); the deprecated `urls` matches substrings and over-blocks.
    First match wins, so `NEVER_BLOCKED_PATTERNS` (anti-bot and captcha vendors, `block=False`)
    always come first. Never write `/*?*`: after `*` the `?` is a modifier, not the query.
    WebSockets and trackers are deliberately not blockable; readiness ignores tracker requests
    instead. The tracker list in `app/browser/data/` is generated by
    `scripts/update_tracker_domains.py`, never edited by hand or fetched at runtime.
  - A known unfixable tell: WebGL reports SwiftShader, because the VPS has no GPU.
- **zendriver is pinned (0.17.0) and partly used through internals**: `zendriver.core.proxy`
  (`ProxyError`, `UpstreamProxy`, `pipe`, `copy_stream`, `format_authority`, `split_authority`,
  `DEFAULT_PORTS`, `HOP_BY_HOP_HEADERS`, `TIMEOUT`), `zendriver.core.connection.ProtocolException`,
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
- Inject them via `runtime.Adapters` (`launcher`, `fetcher_factory`, `resolver`, `pause`,
  `wall_clock`, `clock`, `chance`). Everything in between runs for real.
- `test_http_fetcher.py` and `test_egress.py` run the real fetcher and egress proxy against local
  HTTP servers.
- Async tests use `@pytest.mark.anyio`. `conftest.py` sets the readiness poll interval and the
  base quiet window to 0. Clocks are injected and readiness windows are module constants that
  tests shorten with `monkeypatch`, so tests do not sleep. `tests/test_readiness.py` drives
  `ScriptedTab` on a `ManualClock` with the production constants; `FakeTab`/`ScriptedTab` can
  emit CDP network events.
- `conftest.py` also zeroes the late-content poll interval, and `make_settings` turns the
  late-content watch off (`LATE_CONTENT_OBSERVE_SECONDS=0`). `tests/test_section_profiles.py`
  turns it on and runs fake Chrome on a `ManualClock` (`Adapters.clock`,
  `FakeLauncher(clock=...)`); `harness.settle()` drains verification and observations.

## Documentation to keep in sync

Update these in the same change:
- README.md: the configuration table (variable, default, purpose) and the API tables.
- CHANGELOG.md: SemVer, with breaking changes listed first.
- An ADR in `docs/adr/` for architectural decisions.
