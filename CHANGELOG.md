# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [2.0.0] - Unreleased

Rebuilt on zendriver with a verified HTTP fast path. All entries are relative to 1.x.

### BREAKING CHANGES

- Renamed to `page-render-service`: image `doublelayer/page-render-service`, compose service
  `page-render-service`. Update pull commands, deployments and compose overrides.
- `POST /render` was removed. Use `POST /api/v1/render`; the request body is unchanged.
- Errors use `{"error": {"code", "message", "traceId"}}` with `UPPER_SNAKE_CASE` codes instead of
  `{"detail": {"error", "detail"}}`.
- Validation errors return `400 VALIDATION_ERROR` with per-field details instead of `422`.
- Unknown request fields are rejected with `400`. `timeout` must be between 5 and 120.
- `selector` and `wait_for` must not nest `:has()`, `:not()`, `:is()` or `:where()`, may use at
  most 4 of them and no pseudo-elements; `wait_for` must not use `:-soup-contains()`/`:contains()`.
- Request bodies over 64 KiB get `413 REQUEST_TOO_LARGE`; rendered documents over
  `MAX_RESPONSE_BYTES` characters get `502 RESPONSE_TOO_LARGE`.
- When all browsers are busy, requests wait in a bounded queue; when it is full or the wait times
  out, the service returns `429 SERVICE_BUSY` with `Retry-After` instead of `503 pool_exhausted`.
- Targets resolving to private or reserved addresses are refused with `400 TARGET_NOT_ALLOWED`;
  such redirects inside the browser fail with `502 NAVIGATION_FAILED`.
- Images and CSS are no longer blocked. `BLOCK_IMAGES_AND_CSS` and `WAIT_FOR_COMPLETE_PAGE_LOAD`
  were removed; blocking is opt-in per request with `block_resources`, e.g. `["image", "font"]`.
- `HOME_PROXY` no longer carries every browser (1.x routed all of them through it). It is used for
  requests with `use_proxy: true` and, unless `AUTO_PROXY_ON_BLOCK=false`, for renders blocked by
  bot protection on the direct route. It must be an `http`, `https`, `socks5` or `socks5h` URL
  with an explicit port.
- The service refuses to start on invalid configuration: API keys shorter than 32 characters or
  starting with `CHANGE_ME`, unknown `LOG_LEVEL`, non-numeric, `nan`/`inf` or out-of-range numbers,
  a malformed `HOME_PROXY`, or `ENABLE_VNC=true` with `HEADLESS=true`.
- The noVNC view is off by default. It needs `ENABLE_VNC=true` and `VNC_PASSWORD`, and is served
  only under `/vnc` on the API port. `NOVNC_PREFIX` and `/novnc/...` were removed (`404`).
- Port 6080 is no longer published or exposed; websockify listens on `127.0.0.1` inside the
  container. Remove SSH tunnels or proxy rules that pointed at it.
- `/docs`, `/redoc` and `/openapi.json` are off by default (`404`). `ENABLE_DOCS=true` serves them
  behind HTTP Basic auth (password = API key).
- `docker-compose.yml` publishes port 8000 on `127.0.0.1` only (`API_BIND`), because Docker's port
  publishing bypasses host firewalls. Set `API_BIND` if the reverse proxy runs on another host.
- After 10 wrong API keys within 5 minutes, further wrong keys from that client address get
  `429 TOO_MANY_AUTH_FAILURES` with `Retry-After` instead of `401`; a valid key always passes.
  Behind a reverse proxy set `TRUSTED_PROXY_IPS` to the proxy's address.
- `/health/detail` returns `version`, `pool`, `verdicts`, `clearance`, `profiles` and
  `proxy_hosts` instead of `workers_busy`/`workers_total`.

### Added

- `mode: "auto" | "browser"`. `auto` serves a site section over plain HTTP (curl_cffi with
  Chrome's TLS fingerprint) only after browser renders and HTTP fetches of `VERDICT_MIN_SAMPLES`
  distinct pages matched, every number included; one mismatch makes it browser-only (ADR 0001,
  ADR 0007).
- Response headers `X-Render-Engine`, `X-Render-Stable`, `X-Render-Ready-Reason`,
  `X-Render-Profile`, `X-Render-Route`, `X-Final-Url`, `X-Upstream-Status` and `X-Request-ID`.
- `block_resources`: `false` (default) or a list of `image`, `font`, `media`, `stylesheet`.
  Anti-bot and captcha vendors are never blocked (ADR 0004).
- Several API keys (`API_KEYS`, one per client app); `API_KEY` is still accepted.
- A local egress proxy for Chrome and the HTTP client that applies the SSRF rules to every
  connection and pins the vetted address against DNS rebinding, also towards `HOME_PROXY`.
- Anti-bot challenge detection (vendor markup and titles, human-verification phrases, challenge
  SDKs on small pages): a challenge is never returned as content but fails with
  `502 TARGET_BLOCKED`.
- Clearance reuse: allow-listed anti-bot clearance cookies are carried to the next render of the
  same site and egress route (`CLEARANCE_REUSE`, `CLEARANCE_MAX_AGE_SECONDS`; ADR 0003).
- Automatic proxy escalation: with `HOME_PROXY` set, a direct browser render whose challenge
  persists for `AUTO_PROXY_CHALLENGE_SECONDS` (default 8 s) is rendered once more through
  `HOME_PROXY` with the remaining `timeout`, and the host is remembered for
  `AUTO_PROXY_TTL_SECONDS` (default 6 hours) so later requests start on the proxy and skip the
  HTTP fast path. `X-Render-Route: direct|proxy`, `route=` and `escalated=` in the timing log,
  and `proxy_hosts.entries` in `/health/detail` show it. `AUTO_PROXY_ON_BLOCK=false` turns it
  off (ADR 0008).
- Learned section profiles: per site section the service learns how long content takes,
  including content that appears after a long silence, and makes later renders wait long enough
  (`LATE_CONTENT_OBSERVE_SECONDS`, `PROFILE_OBSERVE_SAMPLE_RATE`, `PROFILE_TTL_SECONDS`;
  ADR 0006).
- Per-host concurrency limit (`MAX_CONCURRENCY_PER_HOST`), browser recycling
  (`BROWSER_MAX_PAGES`, `BROWSER_MAX_AGE_SECONDS`) and a watchdog for hung browsers.
- `BROWSER_LOCALE` and `BROWSER_TIMEZONE` set the browser's language and timezone.
- `GET /vnc` frames the noVNC viewer from the same origin, so the live view works at
  `https://<domain>/vnc` behind a TLS-terminating proxy; a signed 8-hour session cookie
  authenticates its files and WebSocket (ADR 0002).
- `TRUSTED_PROXY_IPS`: reverse proxies whose `X-Forwarded-For` identifies the client.
- Security headers on every response: `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options`, `Content-Security-Policy: frame-ancestors 'self'`.
- One `Render timing` log line per render request, also for failures, with the duration of each
  phase and the readiness outcome. It replaces the `Rendered ... in ...s` line.

### Changed

- Browser engine: zendriver instead of botasaurus. Every request runs in a fresh, isolated
  browser context; only allow-listed clearance cookies are carried over.
- Readiness is event-based and adaptive (ADR 0005): a page is returned once no content request
  is in flight and no new visible text appeared for a quiet window of 0.5 to 3 s, the document is
  loaded, no challenge or loading placeholder is shown and the `wait_for` element exists. Ad and
  analytics requests (a shipped list of up to 1000 domains) never hold a render back.
- `wait_for` is a hint, not a fixed wait: once the element exists the page is returned within
  1 s, even if it keeps changing (`X-Render-Stable: false`). `timeout` is an upper bound for the
  whole request, HTTP attempt included.
- Default `MAX_WORKERS` is 2 (was 3). The compose defaults target a Hetzner CX23
  (`MEM_LIMIT=3g`, `CPU_LIMIT=1.8`).
- The image runs as a non-root user with tini, desktop fonts and a pinned base image. The compose
  file runs it read-only with dropped capabilities, resource limits, a 40 s stop grace period and
  rotated logs.
- Log timestamps are UTC in ISO 8601.
- CI runs lint and tests before building the image.

### Fixed

- Markdown output no longer contains the text of `script`, `style`, `noscript`, `template` and
  `svg` elements.

### Security

- Chrome no longer inherits `API_KEYS`, `API_KEY`, `HOME_PROXY`, `PROXY` or `VNC_PASSWORD`, and a
  managed Chrome policy blocks `file://` URLs, so a VNC user cannot open container files.
- The VNC server requires a password and is reachable only through the authenticated API port.
- Logged target URLs omit credentials and replace the query string with `***`; `X-Final-Url`
  omits credentials.

## [1.x]

The original renderer on botasaurus, before this changelog was introduced.
