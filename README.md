# page-render-service

A scraping API that returns the **fully rendered** HTML (or Markdown) of any URL. Many client
applications can use it at the same time. It is built to run on a small VPS (for example a
Hetzner CX23 with 2 vCPU / 4 GB) and to look like a real desktop Chrome to the sites it visits.

- **Real Chrome via [zendriver](https://github.com/cdpdriver/zendriver)**: CDP-based, with no
  WebDriver traces. Each request runs in its own fresh browser context, so no cookies or storage
  leak between client apps. The one exception are allow-listed anti-bot clearance cookies, which
  are reused briefly per egress route (see [Clearance reuse](#clearance-reuse)).
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
cp .env.example .env            # then set API_KEYS (at least 32 characters each)
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

The service decides on its own how to render a page and when it is complete. The minimal request
is all most callers need:

```json
{"url": "https://example.com"}
```

[docs/render-request.md](docs/render-request.md) is a compact request reference with a full
example, the request headers and the response headers.

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Absolute http(s) URL, at most 2048 characters. |
| `format` | `html` \| `markdown` | `html` | Output format. |
| `selector` | string | - | CSS selector of the element to return instead of the whole page (aliases: `element`, `target`). See [Selector limits](#selector-limits). |

Optional expert overrides. Leave them out unless you have a reason; the service picks the right
behaviour by itself.

| Field | Type | Default | Description |
|---|---|---|---|
| `wait_for` | string | - | Hint: CSS selector that must exist before the page counts as rendered, for content the page loads late. Once it is there, the page is returned within 1 s even if it keeps changing (`X-Render-Stable: false`). A missing element is awaited until `timeout`. Also required in HTTP results. See [Selector limits](#selector-limits). |
| `mode` | `auto` \| `browser` | `auto` | `auto` uses plain HTTP only where it is verified to be complete. `browser` always uses Chrome. |
| `use_proxy` | bool | false | Route this request through `HOME_PROXY` from the start. Usually not needed: with `HOME_PROXY` set, a direct render blocked by bot protection is retried through it automatically (see [Automatic proxy escalation](#automatic-proxy-escalation)). |
| `timeout` | int | 30 | Maximum time in seconds (5-120) the service may spend rendering; it returns as soon as the page is complete. Time spent in the queue is not included. The HTTP fast path may use 30% of it (at most 8 s, all redirects together); a browser render after a failed fast path gets the rest. |
| `block_resources` | `false` \| list | false | Browser only. Request kinds Chrome skips: a non-empty list without duplicates of `image`, `font`, `media`, `stylesheet`, e.g. `["image", "font"]`. See [Resource blocking](#resource-blocking). |

Unknown fields are rejected with `400 VALIDATION_ERROR` naming the field. Request bodies over
64 KiB are refused with `413 REQUEST_TOO_LARGE` before authentication; a valid request is far
smaller.

#### Selector limits

`selector` and `wait_for` are matched by soupsieve in a thread that cannot be cancelled, where
nested pseudo-classes can run for minutes. Both are therefore rejected with
`400 VALIDATION_ERROR` when they nest `:has()`, `:not()`, `:is()` or `:where()` inside each other
(e.g. `div:has(p:has(a))`), use more than 4 of them, or use a pseudo-element such as `p::before`.
`wait_for` is checked by Chrome's `querySelector`, so it must not use soupsieve's own
`:-soup-contains()` or `:contains()` either.

#### Resource blocking

Blocking is opt-in per request, because a browser that never loads certain requests is itself a
bot signal. Ad and analytics requests cannot be blocked and need not be: readiness ignores them,
so they never slow a render down (see [Completeness guarantee](#completeness-guarantee)). Pick
the kinds by their trade-off:

| Kind | Skips | Trade-off |
|---|---|---|
| `image` | URLs whose path ends in `.png .jpg .jpeg .gif .webp .avif .svg .ico .bmp` | Visible to CDN-based bot protection, which sees a client that loads HTML but no images. Content that a lazy loader inserts after an image loads may be missing. |
| `font` | `.woff .woff2 .ttf .otf .eot` | Visible to CDN-based bot protection. Icon fonts render as empty boxes. |
| `media` | `.mp4 .m4v .mov .webm .ogv .mp3 .m4a .aac .ogg .oga .opus .wav .flac` | Visible to CDN-based bot protection. Players may show errors instead of their captions or metadata. |
| `stylesheet` | `.css` | **Strongest warning.** Easy to detect, and pages whose scripts depend on layout (visibility checks, infinite scroll, lazy loading by viewport) may render less content or none. |

- Patterns match the end of the URL path with or without a query (`app.css?v=3`), on any host
  and port, case-sensitively. A URL that merely contains `.css` elsewhere, or a host such as
  `shop.icon.de`, is not blocked.
- Requests to anti-bot and captcha vendors (Cloudflare challenges and
  `/cdn-cgi/challenge-platform/`, DataDome, HUMAN/PerimeterX, AWS WAF, hCaptcha, reCAPTCHA,
  Arkose Labs, Imperva) are never blocked, whatever the kinds. Blocking any part of a challenge turns it into a hard block.
- WebSockets cannot be blocked: Chrome does not reliably block their handshakes, readiness does
  not wait for them, and what they carry is often the content itself.
- A render with any blocking is never used for [HTTP verification](#completeness-guarantee) or
  for [learned section profiles](#learned-section-profiles).
- The render timing log shows the kinds as `blocked=...`.

**Tracker list.** `app/browser/data/tracker_domains.txt` holds up to 1000 domains; readiness
ignores requests to each of them and its subdomains, except the domain the requested page itself
is on. Nothing on the list is blocked: the requests still go out, as they do for a real visitor,
they just never hold the render back. The list is generated from the tracking- and ad-server
sections of [EasyPrivacy and EasyList](https://easylist.to) (GPLv3 or CC BY-SA 3.0), keeping only
rules that block a whole domain, dropping domains the lists exempt somewhere, and ranking the
rest by the [Tranco](https://tranco-list.eu) top-1M list. Tag managers (Google Tag Manager,
Tealium), consent managers, experimentation and personalisation services, fraud detection and the
anti-bot vendors above are never listed: pages load content, consent state or test variants
through them, so readiness must wait for them. The service reads the file once at startup and
refuses to start if it is missing, empty or malformed; it never downloads anything at runtime. To
refresh the list (maintainers, network needed), run from the repository root and commit the
result:

```bash
python -m scripts.update_tracker_domains
```

**Success:** `200` with the content as `text/html` or `text/markdown`, plus these headers:

| Header | Meaning |
|---|---|
| `X-Render-Engine` | `http` or `browser` |
| `X-Render-Stable` | `false` if the content was still changing when it was returned (timeout reached, or `wait_for` present on a page that never settled) |
| `X-Render-Ready-Reason` | Why the content counted as complete: `settled`, `wait-for-found`, `load-budget-expired` or `deadline` for browser renders (see [Completeness guarantee](#completeness-guarantee)), `verified-http` for the HTTP fast path |
| `X-Render-Profile` | Whether readiness floors learned from earlier renders of the same site section applied: `learned` (the wait may have been lengthened, never shortened), `cold` (section unknown), `n/a` for the HTTP fast path (see [Learned section profiles](#learned-section-profiles)) |
| `X-Render-Route` | Egress route the content came through: `direct` (the server's own IP) or `proxy` (`HOME_PROXY`: `use_proxy`, an automatic retry, or a host remembered as needing it; see [Automatic proxy escalation](#automatic-proxy-escalation)) |
| `X-Final-Url` | URL after redirects, percent-encoded, without credentials |
| `X-Upstream-Status` | HTTP status the target returned for the main document (`0` if unknown). A 404 page is still returned as content. |
| `X-Request-ID` | Trace id. Pass your own `X-Request-ID` (1-64 characters of `A-Z a-z 0-9 . _ : -`) to correlate logs; any other value is replaced by a generated id. |

Every response, including errors (`500` too), also carries `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: SAMEORIGIN` and
`Content-Security-Policy: frame-ancestors 'self'`.

**Errors** use one envelope:
`{"error": {"code": "...", "message": "...", "traceId": "..."}}`. Validation errors also carry
`fields: [{"field", "message"}]`.

| Status | Code | Condition |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Invalid body (per-field details), including [selector limits](#selector-limits) |
| 400 | `TARGET_NOT_ALLOWED` | Non-http(s) URL, or a host resolving to a private or reserved address |
| 400 | `PROXY_NOT_CONFIGURED` | `use_proxy` without `HOME_PROXY` |
| 401 | `UNAUTHORIZED` | Missing or unknown API key (all authenticated endpoints) |
| 404 | `ELEMENT_NOT_FOUND` | `selector` matched nothing |
| 404 | `NOT_FOUND` | Unknown path, a disabled `/docs` or `/vnc` route, or a disallowed `/vnc/app/` path |
| 405 | `METHOD_NOT_ALLOWED` | Wrong HTTP method for an existing path |
| 413 | `REQUEST_TOO_LARGE` | Request body over 64 KiB; refused before authentication |
| 429 | `SERVICE_BUSY` | Queue full or no capacity in time; honour `Retry-After` |
| 429 | `TOO_MANY_AUTH_FAILURES` | 10 wrong API keys from this client within 5 minutes (all authenticated endpoints); honour `Retry-After` |
| 500 | `INTERNAL_ERROR` | Unexpected failure; the `traceId` identifies it in the service log |
| 502 | `NAVIGATION_FAILED` | DNS, connection or TLS failure, a redirect to a forbidden address, or a page that could not be read (its tab crashed, or no observation succeeded before `timeout`) |
| 502 | `TARGET_BLOCKED` | An anti-bot challenge did not resolve within the timeout; with automatic proxy escalation, also not on the retry through `HOME_PROXY` |
| 502 | `RESPONSE_TOO_LARGE` | The rendered document has more than `MAX_RESPONSE_BYTES` characters |
| 502 | `VNC_UNAVAILABLE` | `/vnc/app/...`: the in-container noVNC server did not answer |
| 504 | `TIMEOUT` | `wait_for` never appeared, the target did not start responding within `timeout`, or the browser stopped responding |

`WS /vnc/websockify` cannot answer with a body: it closes with code `1008` when authentication
or the `Origin` check fails, and `1011` when noVNC does not answer.

### Other endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /health` | none | Liveness probe |
| `GET /health/detail` | Bearer | Pool utilisation (`observing`: workers held by a late-content observation), restarts, learned verdicts, number of learned section profiles (`profiles.entries`), stored clearance cookies and hosts remembered as needing `HOME_PROXY` (`proxy_hosts.entries`) |
| `GET /vnc` | Basic (password = API key) | noVNC live view page; sets the viewer session cookie. `404` unless `ENABLE_VNC=true` |
| `GET /vnc/app/{path}` | session cookie or Basic | noVNC files relayed from the container; `404` for unknown or disallowed paths, `502 VNC_UNAVAILABLE` if noVNC does not answer |
| `WS /vnc/websockify` | session cookie or Basic, same `Origin` | VNC stream relayed from the container |
| `GET /docs`, `/redoc`, `/openapi.json` | Basic (password = API key) | API documentation; `404` unless `ENABLE_DOCS=true` |

All authenticated endpoints share the failed-login lockout: after 10 wrong API keys (Bearer or
Basic) from one client address within 5 minutes, further wrong keys from that address get
`429 TOO_MANY_AUTH_FAILURES` with `Retry-After` instead of `401` until the window has passed. A
valid key always passes and is not counted, so clients sharing an address with an attacker are
never locked out; guessing a key is infeasible, because keys have at least 32 characters.
Requests without any credentials are not counted. The client address is the TCP peer;
`X-Forwarded-For` is only used when the peer is listed in `TRUSTED_PROXY_IPS`.

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
   - Only stable status-200 browser renders without `block_resources` are references. Each is
     first watched for late content (see [Learned section profiles](#learned-section-profiles)).
     Only if that watch ran to its end and saw no growth is the same URL fetched once over HTTP,
     after a random 1-3 s pause. A skipped, cut short or failed watch verifies nothing; with
     `LATE_CONTENT_OBSERVE_SECONDS=0` no section is ever verified.
   - The visible texts of both documents are compared.
   - A render matches when the HTTP document contains at least `MIN_TEXT_COVERAGE` (default
     90%) of the browser's words and text length, and **every number** the browser showed, at
     least as often (prices, stock levels and counts are typical client-rendered content). A
     number keeps its separators: `1.299,99` is one number, not `1`, `299` and `99`.
   - The section is trusted after `VERDICT_MIN_SAMPLES` matches of **different pages**. Paths
     are compared case-insensitively, with `;` parameters, duplicate and trailing slashes and
     escaped unreserved characters normalised away; renders with identical visible text count
     as one page.
   - A section with a single page (such as a homepage) is also trusted after
     `VERDICT_MIN_SAMPLES` matches of that page at least `VERDICT_SAME_PAGE_INTERVAL_SECONDS`
     apart. Within the interval the page is not fetched again.
   - A single mismatch marks the section browser-only.
   - Verdicts expire after `VERDICT_TTL_SECONDS`.
   - An HTTP response that redirects out of a verified section is not used.
2. **This very response passes every check.** The checks are: status 200 and HTML; no anti-bot
   challenge; no empty SPA mount point (`#root`, `#app`, `#__next`, ...); no "enable JavaScript"
   notice or meta refresh; enough visible text; and every `wait_for`/`selector` element present.
   If any check fails, the request is rendered in the browser and the section becomes
   browser-only.

In the browser, the service decides by itself when a page is complete and returns it as soon
as that is provable, never earlier. The page is checked every 100 ms and counts as rendered when
all of these hold at the same time:

- The document is loaded: `readyState` is `complete`, or it has been `interactive` for 2 s (a
  subresource that never finishes does not hold the page back).
- No challenge is shown, and the `wait_for` element exists.
- No visible loading placeholder is on screen: an `aria-busy="true"` region, an element whose
  class or id has `skeleton`, `spinner`, `loader`, `loading` or `placeholder-shimmer` as its own
  token, or a bare "Loading...", "Wird geladen" or "Lädt…" text. Placeholders below the first
  screen are ignored, because content that loads on scroll never arrives.
- No content request is in flight. Chrome reports every request to the service, also the ones
  still open. Only documents (including iframes), scripts, XHR and fetch count. Images, fonts,
  stylesheets, media, beacons, WebSockets, event streams, requests to the
  [tracker list](#resource-blocking) and requests open longer than 5 s (long polls) are ignored.
- Neither a content request nor DOM growth happened for the **quiet window**. DOM growth means
  new visible text; text a carousel rotates back in was seen before and does not count, and
  attribute changes do not count either. The page is watched from an isolated script world the
  page itself cannot see.

The quiet window adapts to the page. It starts at 0.5 s and grows to twice the longest silence
seen between two content events of this render, up to 3 s: a page that loads in one burst
returns half a second after its last request, a page that fetches in waves is given the time its
own rhythm suggests. Once the `wait_for` element is present, the window is capped at 1 s.

After 60% of the timeout (the load budget), network activity is ignored as an emergency brake,
so pages that poll for data every second still finish on their DOM
(`X-Render-Ready-Reason: load-budget-expired`). If the content is still growing at `timeout`,
the page is returned with `X-Render-Stable: false`. A page that sets content only from a timer
after a long silence is handled by [learned section profiles](#learned-section-profiles); the
first render of such a section can still be returned before that content exists, so pass
`wait_for` where you know the element.

### Learned section profiles

The service learns per site section (host, first path segment, path depth) how long its pages
take, so slow sites get enough time without any request field, and fast sites stay fast.

- Each stable status-200 browser render without `block_resources` records when its content last
  grew and the longest silence between two content events. The last 20 renders per section are
  kept, for `PROFILE_TTL_SECONDS` counted from the section's first render.
- The next render of the section cannot be declared ready before the 90th percentile of the
  last-growth times plus 0.5 s, and its quiet window is at least twice the 90th percentile of
  the longest silences (at most 3 s, also when the `wait_for` element is present).
- **Learned data may only lengthen waits.** Only what the current render observes ends a wait;
  a page that settles later than learned still waits for its own quiet window.
- **Late content.** To learn about content that appears after a long silence, a returned page
  is watched for `LATE_CONTENT_OBSERVE_SECONDS` more in the background. If its content grows,
  the next render of the section waits that long, and the section becomes browser-only for the
  HTTP fast path. Every section is watched for its first 2 renders, later at
  `PROFILE_OBSERVE_SAMPLE_RATE`; a section still waiting for an HTTP verdict is watched on every
  render, because only a cleanly watched render may be verified.
- **Cost.** The watch keeps its browser worker, but only while no request is waiting: it does
  not start when requests are queued, and a request that needs a browser stops it at once. A
  failed watch is logged and never changes a response that has already been sent.

On `quotes.toscrape.com/js-delayed/` the first render returns after about 0.5 s without the
quotes (`X-Render-Profile: cold`); the watch sees them appear after about 10 s, and from then on
renders of that section return after about 10.6 s with the quotes (`X-Render-Profile: learned`).
Profiles are shared by all clients and held in memory per process.

### Anti-bot challenges

A challenge page is never returned as content. In the browser the service keeps waiting while a
challenge is shown, because many challenges solve themselves and reload the page. If it is still
shown at `timeout`, the request fails with `502 TARGET_BLOCKED`. With `HOME_PROXY` configured,
a direct render gives up on a challenge earlier and is retried through the proxy (see
[Automatic proxy escalation](#automatic-proxy-escalation)). On the HTTP fast path a
challenge sends the request to the browser and makes the section browser-only. A page counts as a challenge when:

- it carries the markup of a known vendor block page (Cloudflare, DataDome, PerimeterX, Akamai,
  Imperva), whatever its size; or
- its whole title is a vendor challenge title ("Just a moment...", "Access denied", "Attention
  Required! | Cloudflare", optionally with a site name after or before `-`, `|` or `:`) and it
  has little visible text (under 1000 characters); or
- it has little visible text (under 1000 characters) **and** either
  - a human-verification phrase in its title or an `h1`/`h2`, in German, English, French or
    Spanish ("Ich bin kein Roboter", "Are you human?", "Verify you are human",
    "Checking your browser", "Êtes-vous un humain ?", "No soy un robot", ...), or
  - the script or widget of a challenge SDK: AWS WAF (`awswaf.com` + `challenge.js`/`captcha.js`,
    `AwsWafIntegration`), hCaptcha, Cloudflare Turnstile, or a reCAPTCHA challenge frame or
    checkbox.

Only whole phrases count, so titles such as "Robot vacuum cleaners" or "Human Resources" do not,
and neither do "WordPress Security Checklist" or a song page titled "Just a Moment - Song by X".
The size limit exists because these SDKs also run on normal pages (AWS WAF token acquisition, a
Turnstile or hCaptcha contact form, the reCAPTCHA v3 badge); a page with real content is never
treated as a challenge because of them. The rules are generic; there are no per-site rules.

### Clearance reuse

Once a browser render passes a challenge, the vendor sets a clearance cookie (for example
`aws-waf-token` or `cf_clearance`). With `CLEARANCE_REUSE=true` (default) the
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

### Automatic proxy escalation

Datacenter addresses are challenged by strong bot protection whatever the browser does. With
`HOME_PROXY` set and `AUTO_PROXY_ON_BLOCK=true` (default), a request that did not set
`use_proxy` handles a block on its own (ADR 0008):

1. The direct render gives up on a challenge that is still shown `AUTO_PROXY_CHALLENGE_SECONDS`
   (default 8 s) after it first appeared, instead of waiting for `timeout`. A challenge that
   solves itself within that time is not affected.
2. The page is rendered once more through `HOME_PROXY` with what is left of `timeout`. If less
   than 1 s is left, the request fails with `502 TARGET_BLOCKED` without a retry; such a short
   request gets the whole `timeout` for the challenge on the direct route, as without escalation.
   If the proxy render is blocked too, the request fails with `502 TARGET_BLOCKED`.
3. A host that got through only by proxy is remembered for `AUTO_PROXY_TTL_SECONDS` (default
   6 hours). Later requests to it go through `HOME_PROXY` from the start and skip the HTTP fast
   path, because curl_cffi uses the direct route. A successful direct render does not end this;
   only the lifetime does, after which the host is tried directly again.

Details:

- The response says which route it took (`X-Render-Route: direct|proxy`); the timing log line
  carries `route=` and `escalated=true|false`.
- One retry per request at most. The per-host slot is kept across both attempts. The browser is
  not: the blocked attempt returns it to the pool and the retry queues again like a new request,
  so a full queue answers `429 SERVICE_BUSY`.
- Renders through the proxy never verify a section for the HTTP fast path, and the automatic
  retry teaches no section timing either.
- Clearance cookies stay per route: a token earned through `HOME_PROXY` is reused only there.
- Every escalated and remembered render runs over the home connection: its upload bandwidth and
  its IP address are what the target sees. Set `AUTO_PROXY_ON_BLOCK=false` to use `HOME_PROXY`
  only for requests with `use_proxy: true`.

### `wait_for`

If you know an element that only exists once the content you need is loaded, pass it as
`wait_for`. It is the strongest signal for both engines. Use `mode: "browser"` to skip HTTP
entirely.

**How long `wait_for` waits:**

| Situation | Result |
|---|---|
| Element present, page settles | Returned as soon as the quiet window (at most 1 s) has passed |
| Element present, page never settles (carousels, tickers) | Returned 1 s after the element appeared on the loaded page, `X-Render-Stable: false` |
| Element missing | `504` when `timeout` is reached |

There is no early give-up for a missing element. A quiet page does not prove the element will
not come: content scheduled by a timer or pushed over a websocket arrives without prior activity.
On `quotes.toscrape.com/js-delayed/` the content appears after about 10 s of complete silence, so
an idle cut-off would have reported it as missing.

## Render timing log

Every `POST /api/v1/render` writes one `INFO` line on the `render.api` logger, also for
failures, so slow requests can be broken down. Target URLs in this and all other log lines show
scheme, host and path only: credentials are dropped and a query string is replaced by `***`.

```
Render timing traceId=4f1c... url=https://example.com/a outcome=ok engine=browser host_wait_ms=0 queue_ms=2 context_ms=41 navigate_ms=1830 readiness_ms=21950 read_ms=64 output_ms=35 total_ms=23930 blocked=none route=direct escalated=false profile=learned min_ready_ms=1300 readiness_end=load-budget-expired challenge_polls=0 quiet_ms=3000 inflight_ignored=37 clearance=none
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
| `blocked` | Request kinds the browser was told to skip, sorted (`font,image`), or `none`. Present once the request passed the URL checks. |
| `route` | Egress route of the result: `direct` or `proxy`. Sent as `X-Render-Route` when a page is returned. |
| `escalated` | `true` if the direct render was blocked and retried through `HOME_PROXY` (see [Automatic proxy escalation](#automatic-proxy-escalation)), else `false`. |
| `profile` | Browser renders only: `learned` if floors learned for the section applied, else `cold` (see [Learned section profiles](#learned-section-profiles)). Sent as `X-Render-Profile`. |
| `min_ready_ms` | Browser renders only: earliest time after navigation the page could be declared ready (`0` when cold). |
| `readiness_end` | Why the readiness wait stopped: `settled` (quiet before 60% of `timeout`), `load-budget-expired` (quiet only once network activity was ignored), `wait-for-found` (element present, DOM never settled), `deadline` (still changing at `timeout`), `challenge`, `challenge-persisted` (a challenge outlasted `AUTO_PROXY_CHALLENGE_SECONDS` on the direct route), `element-missing`, or `interrupted` (the browser hung). After an escalation it describes the proxy render. Sent as `X-Render-Ready-Reason` when a page is returned. |
| `challenge_polls` | Readiness polls that saw an anti-bot challenge, including one that later cleared. |
| `quiet_ms` | Quiet window of the last readiness poll (500 to 3000; at most 1000 once the `wait_for` element is present, unless the section's learned floor is higher). |
| `inflight_ignored` | Requests that did not hold the page back: non-content kinds, tracker hosts, long polls and event streams. |
| `clearance` | Clearance cookie reuse (browser renders only, absent with `CLEARANCE_REUSE=false`): `reused` (stored cookies were set and not rejected), `stored` (none were set, the render earned new ones), `dropped` (stored cookies were set, but the challenge stayed, so they were forgotten), or `none`. |

Phases that did not run are left out. A high `readiness_ms` with `readiness_end=deadline` or
`load-budget-expired` points to a page that keeps changing or polling; pass `wait_for` to end
the wait as soon as the needed content exists. A high `queue_ms` means the pool is too small for
the load.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `API_KEYS` | required | Comma-separated API keys, one per client app, each at least 32 characters and not starting with `CHANGE_ME`. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. `API_KEY` (single key) is still accepted. |
| `MAX_WORKERS` | `2` | Browser instances. 2 on a CX23 (4 GB / 2 vCPU), 3 on a CX33 (8 GB / 4 vCPU). |
| `HEADLESS` | `false` | `false`: headed Chrome on Xvfb (harder to detect). `true`: `--headless=new`. |
| `HOME_PROXY` / `PROXY` | - | Upstream proxy for `use_proxy` requests and for [automatic proxy escalation](#automatic-proxy-escalation): `http://`, `https://`, `socks5://` or `socks5h://` with host and an explicit port (1-65535), credentials allowed; Chrome never sees them. |
| `AUTO_PROXY_ON_BLOCK` | `true` | Retry a direct browser render that is blocked by bot protection through `HOME_PROXY` and remember the host (see [Automatic proxy escalation](#automatic-proxy-escalation)). Has effect only when `HOME_PROXY` is set. |
| `AUTO_PROXY_CHALLENGE_SECONDS` | `8` | How long a challenge may persist on the direct route before the service gives up on it and retries through `HOME_PROXY` (2-60). |
| `AUTO_PROXY_TTL_SECONDS` | `21600` | How long a host is remembered as needing `HOME_PROXY` (60-604800). |
| `BROWSER_LOCALE` | `de-DE` | Browser language and `Accept-Language`. Must match the egress IP. |
| `BROWSER_TIMEZONE` | `Europe/Berlin` | Browser timezone. Must match the egress IP. |
| `BROWSER_MAX_PAGES` | `150` | Restart a browser after this many renders. |
| `BROWSER_MAX_AGE_SECONDS` | `2700` | Restart a browser after this age (at least 60). |
| `QUEUE_TIMEOUT_SECONDS` | `20` | How long a request waits for a per-host slot and for a free browser (each). |
| `MAX_QUEUE_SIZE` | `8` | Requests allowed to wait for a browser; beyond that `429` is returned at once. |
| `MAX_CONCURRENCY_PER_HOST` | `2` | Simultaneous requests per target host, HTTP fast path and browser alike. |
| `HTTP_FIRST_ENABLED` | `true` | Enable the verified HTTP fast path. |
| `VERDICT_MIN_SAMPLES` | `2` | Matches of different pages (or of one page over time, see next row) needed before a section is served over HTTP. |
| `VERDICT_SAME_PAGE_INTERVAL_SECONDS` | `600` | Minimum spacing of repeated matches of the same page, so a single-page section can earn the HTTP fast path (at least 60, below `VERDICT_TTL_SECONDS`). |
| `VERDICT_TTL_SECONDS` | `21600` | Lifetime of a learned verdict (at least 60). |
| `MIN_TEXT_COVERAGE` | `0.9` | Share of the browser's words and text length the HTTP document must contain (0-1). |
| `MAX_RESPONSE_BYTES` | `10485760` | Size cap for HTTP responses (at least 1024); larger responses go to the browser. A browser render whose document has more characters fails with `502 RESPONSE_TOO_LARGE`. |
| `LATE_CONTENT_OBSERVE_SECONDS` | `10` | How long a returned page is watched for late content to learn its section's timing (0-30, `0` disables the watch; timing is still learned from the renders themselves). The watch holds its worker only while no request waits. Only a cleanly watched render can verify a section, so `0` also disables the HTTP fast path: no section becomes `HTTP_SUFFICIENT`. |
| `PROFILE_OBSERVE_SAMPLE_RATE` | `0.05` | Share of renders watched once a section had its first 2 watched renders (0-1). |
| `PROFILE_TTL_SECONDS` | `21600` | Lifetime of a learned section profile, counted from its first render (60-604800). |
| `CLEARANCE_REUSE` | `true` | Reuse anti-bot clearance cookies between browser renders of the same site and egress route (see [Clearance reuse](#clearance-reuse)). |
| `CLEARANCE_MAX_AGE_SECONDS` | `240` | Longest time a stored clearance cookie is reused (1-3600); a shorter cookie expiry wins. Below the typical 300 s token immunity. |
| `ALLOW_PRIVATE_TARGETS` | `false` | Allow private, loopback and link-local targets. Local development only. |
| `ENABLE_VNC` | `false` | Start x11vnc and noVNC. Headed mode only: together with `HEADLESS=true` the service refuses to start. |
| `VNC_PASSWORD` | - | Required when `ENABLE_VNC=true`. VNC uses at most 8 characters. |
| `VNC_PORT` | `6080` | Container-internal port of the noVNC server on `127.0.0.1`. Never published; `/vnc` relays it. |
| `ENABLE_DOCS` | `false` | Serve `/docs`, `/redoc` and `/openapi.json` behind HTTP Basic auth (password = API key). `false`: `404`. |
| `TRUSTED_PROXY_IPS` | - | Comma-separated IPs or CIDR ranges of reverse proxies whose `X-Forwarded-For` is trusted to identify clients for the failed-login lockout. List only the proxy's own address as the container sees it (for a proxy on the host connecting to `127.0.0.1:8000`, the gateway of the compose network, e.g. `172.18.0.1`; see `docker network inspect`); a whole network would let any container on it choose its client address. Without it, all clients behind the proxy share one lockout. |
| `BROWSER_SANDBOX` | `false` | Chrome sandbox. It needs user namespaces, which containers usually lack. |
| `CHROME_BIN` | auto | Chrome executable. The image uses `/usr/local/bin/chrome-launcher`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` (case-insensitive); any other value stops the service at startup. Timestamps are UTC. |

Read by `docker-compose.yml` only (from `.env`), not by the service:

| Variable | Default | Purpose |
|---|---|---|
| `API_BIND` | `127.0.0.1` | Host address the API port 8000 is published on. Keep the default when the reverse proxy runs on the same host; see [Deployment](#deployment-on-a-small-vps). |
| `MEM_LIMIT` | `3g` | Container memory limit, including the `/tmp` tmpfs (512 MB). CX33: `6g`. |
| `CPU_LIMIT` | `1.8` | Container CPU limit; must not exceed the host's vCPUs, or Docker refuses to start. CX33: `3.5`. |

## Deployment on a small VPS

- **Server:** the compose defaults target a Hetzner CX23 (2 vCPU / 4 GB): `MAX_WORKERS=2`,
  `MEM_LIMIT=3g`, `CPU_LIMIT=1.8`, leaving room for the OS and the reverse proxy. On a CX33
  (4 vCPU / 8 GB) set `MEM_LIMIT=6g`, `CPU_LIMIT=3.5` and `MAX_WORKERS=3` in `.env`. Raise workers
  only after watching `/health/detail` and memory. The browser pool is CPU-bound before it is
  RAM-bound. The image is amd64 only, so do not use the ARM (CAX) plans.
- **Hardening:** `docker-compose.yml` runs the container read-only, as a non-root user, with all
  capabilities dropped, memory/CPU limits, a 40 s stop grace period (uvicorn finishes running
  renders for up to 30 s) and rotated logs (5 x 20 MB). Port 8000 is the only published port.
  Put TLS and IP allow-listing in a reverse proxy in front of it, and list only that proxy's
  address in `TRUSTED_PROXY_IPS`.
- **Port binding:** port 8000 is published on `127.0.0.1` by default (`API_BIND`), because
  Docker's published ports bypass host firewalls such as ufw. A reverse proxy on the same host
  connects to `127.0.0.1:8000`. If the proxy runs on another machine, set `API_BIND` to the
  private interface address it reaches (e.g. `API_BIND=10.0.0.5`) and restrict that interface to
  the proxy; `0.0.0.0` exposes the plain-HTTP port to everyone.
- **Chrome isolation:** `docker/chrome-launcher.sh` removes `API_KEYS`, `API_KEY`, `HOME_PROXY`,
  `PROXY` and `VNC_PASSWORD` from Chrome's environment before starting it, so a compromised
  renderer or crash dump cannot read them. A managed Chrome policy
  (`/etc/opt/chrome/policies/managed/page-render-service.json`) blocks `file://` URLs, so a VNC
  user cannot open container files such as `file:///proc/self/environ` in the visible browser.
  The policy also makes `chrome://policy` show the browser as managed; pages cannot see it.
- **Egress IP:** Hetzner addresses are well-known datacenter ranges. Strongly protected sites
  (Cloudflare Bot Management, DataDome, Akamai, ...) challenge them whatever the browser
  fingerprint. For those, set `HOME_PROXY` (for example a proxy at home reached over
  WireGuard/Tailscale, or an ISP/residential proxy); blocked hosts are then retried through it
  automatically, or send `use_proxy: true` to start there. Make `BROWSER_LOCALE` and
  `BROWSER_TIMEZONE` match that exit.
- **Debug view:** set `ENABLE_VNC=true` and `VNC_PASSWORD`, then visit `https://<your-domain>/vnc`
  (see [Live view (VNC)](#live-view-vnc)).

### Known limits

- A VPS has no GPU, so WebGL reports a software renderer (SwiftShader). Bot-detection pages can
  see this. It cannot be fixed without a GPU or a patched browser build.
- Content that loads only after scrolling or clicking is not triggered.
- Readiness sees requests and DOM changes, not pending timers. Content that a page inserts from
  a timer after more than the quiet window of complete silence is learned per section by
  watching returned pages; until the first watch of a section finished, such content can be
  missed, and `wait_for` closes that gap. Content later than `LATE_CONTENT_OBSERVE_SECONDS` after
  the return is never learned. Requests of out-of-process (cross-site) iframes are not seen by
  the network tracker; the main document's DOM growth still counts.
- Readiness observes the main document only. Content rendered into a Shadow DOM or a
  same-origin iframe does not count as growth, and a loading placeholder inside one does not hold
  the page back, so such content can be missing; `wait_for` on a main-document element that
  appears with it closes the gap.
- With `use_proxy`, host names are resolved on the VPS and the upstream proxy is given the vetted
  IP address, so it cannot re-resolve a name into its own network. A site with geo-dependent DNS
  is therefore reached at the address the VPS's resolver returns, not the one near the proxy.
- The HTTP fast path re-fetches a URL without cookies shortly after a browser visit. That pattern
  and the curl_cffi Chrome version (which may differ from the installed Chrome) are observable
  by the target. Set `HTTP_FIRST_ENABLED=false` for the most sensitive targets.
- Chrome runs without its sandbox (`BROWSER_SANDBOX=false`), because containers usually lack the
  user namespaces it needs. The container hardening is the isolation boundary.
- Verdicts, section profiles, clearance cookies and hosts remembered as needing `HOME_PROXY` are
  held in memory and are relearned after a restart.
- The failed-login lockout and the VNC session key live in the memory of one process. Running
  several uvicorn workers would split the lockout counters and invalidate cookies across
  workers; the image runs one.
- The service speaks plain HTTP. Without a TLS-terminating proxy in front, API keys and the VNC
  session cookie travel in clear text.

## Local development

```bash
pip install -r requirements-dev.txt
ruff check app tests scripts && ruff format --check app tests scripts
pytest
```

Tests use fakes at the Chrome (CDP), HTTP and DNS boundaries and need no network. To run the
service locally against an installed Chrome:

```bash
API_KEYS=dev-key-at-least-32-characters-long HEADLESS=true ALLOW_PRIVATE_TARGETS=true \
  uvicorn --factory app.main:create_app --port 8000
```

## Architecture

```
app/
  main.py               routes, lifecycle, response mapping
  runtime.py            wiring of all components and their lifecycle
  config.py             environment variables, validated at startup
  api_models.py         request model and validation
  errors.py             error types, envelope and trace id
  openapi_docs.py       documented headers, errors and examples of the OpenAPI schema
  auth.py               API-key checks (Bearer, Basic)
  auth_throttle.py      failed-login lockout
  client_address.py     client address, X-Forwarded-For only from trusted proxies
  body_limit.py         request body size limit, applied before parsing and authentication
  security_headers.py   security headers on every response
  docs_routes.py        guarded Swagger UI, ReDoc and OpenAPI schema
  vnc.py                /vnc routes
  vnc_proxy.py          relay to the in-container noVNC server
  vnc_session.py        signed viewer session cookie
  egress.py             local egress proxies that enforce the SSRF guard on all traffic
  url_guard.py          SSRF rules and address vetting
  timing.py             per-request phase durations for the render timing log
  log_safety.py         target URLs without credentials and query strings for the logs
  logging_config.py     log format (UTC) and level
  scraping/
    scraper.py          engine choice per request: verified HTTP or browser
    host_limiter.py     per-host concurrency limit
    verdicts.py         per-section HTTP verdicts and page identity
    verifier.py         background HTTP verification of browser renders
    learning.py         what one browser render teaches: timing, late content, verification
    profiles.py         learned readiness floors per site section
    clearance.py        store of anti-bot clearance cookies
    proxy_hosts.py      hosts remembered as needing HOME_PROXY (automatic escalation)
  browser/
    session.py          one Chrome instance (zendriver), launch switches
    pool.py             worker pool with bounded wait queue
    worker.py           pool slot that recycles its browser and catches hangs
    page_loader.py      one render in a fresh browser context
    readiness.py        when a page counts as rendered
    network_activity.py in-flight content requests from CDP network events
    activity.py         timeline of content events of one render
    late_content.py     late-content watch after the response
    evaluation.py       JavaScript evaluation without a user gesture
    blocking.py         resource blocking patterns and the tracker list loader
    clearance.py        clearance cookies into and out of a browser context
    data/               tracker_domains.txt
  fetch/http_fetcher.py curl_cffi HTTP fetcher
  content/
    completeness.py     completeness and challenge checks
    text.py             visible text and the browser/HTTP comparison
    output.py           HTML/Markdown output and selector extraction
docker/                 entrypoint, Chrome launcher (timezone/language, secrets removed) and
                        managed Chrome policy (file:// blocked)
scripts/                maintenance scripts (tracker domain list refresh)
docs/adr/               architecture decisions
```

See [CHANGELOG.md](CHANGELOG.md) for changes and the migration from 1.x.
