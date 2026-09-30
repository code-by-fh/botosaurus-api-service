# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [2.0.0] - Unreleased

### BREAKING CHANGES

- The service was renamed to `page-render-service`. The Docker image is now
  `doublelayer/page-render-service` and the compose service is now `page-render-service`. Update
  any deployment, pull command or compose override that references the old image or service name.
- `NOVNC_PREFIX` and the unauthenticated `/novnc/...` proxy were removed; `/novnc/...` returns
  `404`. The live view is served only under `/vnc` through the API port (see Added).
- Port 6080 is no longer published or exposed. websockify listens on `127.0.0.1` inside the
  container only. Remove any SSH tunnel or reverse-proxy rule that pointed at port 6080.
- `/docs`, `/redoc` and `/openapi.json` are off by default and return `404`. Set
  `ENABLE_DOCS=true` to serve them behind HTTP Basic auth (password = API key).
- After 10 wrong API keys within 5 minutes a client address gets `429 TOO_MANY_AUTH_FAILURES`
  with `Retry-After`, even for a correct key. Behind a reverse proxy set `TRUSTED_PROXY_IPS`, or
  all clients share the proxy's address and lockout.
- `POST /render` was removed. Use `POST /api/v1/render`; the request body is unchanged.
- The error body is now `{"error": {"code", "message", "traceId"}}` with `UPPER_SNAKE_CASE` codes.
  It used to be `{"detail": {"error", "detail"}}`.
- Validation errors return `400 VALIDATION_ERROR` with per-field details instead of `422`.
- When capacity is exhausted the service returns `429 SERVICE_BUSY` with `Retry-After`. It used
  to return `503 pool_exhausted` immediately. Requests now wait in a queue first.
- Unknown request fields are rejected. `timeout` must be between 5 and 120.
- Images and CSS are no longer blocked by default. Use `block_resources: true` per request.
  `BLOCK_IMAGES_AND_CSS` and `WAIT_FOR_COMPLETE_PAGE_LOAD` were removed.
- The noVNC view is off by default. It requires `ENABLE_VNC=true` and `VNC_PASSWORD`.
- Targets that resolve to private or reserved addresses are refused (`TARGET_NOT_ALLOWED`).
  Redirects to such addresses inside the browser fail with `NAVIGATION_FAILED`.

### Added

- `POST /api/v1/render`.
- `mode: "auto" | "browser"` with a verified HTTP fast path (curl_cffi, Chrome TLS fingerprint).
  An HTTP result is only returned for site sections proven to deliver the complete page.
- Response headers `X-Render-Engine`, `X-Render-Stable`, `X-Final-Url`, `X-Upstream-Status` and
  `X-Request-ID`.
- A local egress proxy through which Chrome and the HTTP client send all traffic. It enforces the
  SSRF rules on every connection and pins the vetted address against DNS rebinding.
- Several API keys (`API_KEYS`), one per client app.
- `idle_timeout` (with `wait_for`) ends the wait early when the loaded page stays idle without
  the element. With `wait_for`, a page that never settles is returned 3 s after the element
  appeared instead of at the timeout.
- A bounded wait queue, per-host concurrency limits, browser recycling after N pages or M
  minutes, and a watchdog for hung browsers.
- `use_proxy` now really selects the egress per request, through per-context proxies.
- Browser language and timezone follow `BROWSER_LOCALE` and `BROWSER_TIMEZONE`.
- `/health/detail` reports restarts, queue length and learned verdicts.
- The CI runs lint and tests before building the image.
- `GET /vnc` frames the noVNC viewer from the same origin (`/vnc/app/...`, `WS /vnc/websockify`),
  so the live view works at `https://<domain>/vnc` behind a TLS-terminating proxy. A successful
  Basic login sets a signed 8-hour session cookie (HttpOnly, SameSite=Strict, Path=/vnc, Secure
  over HTTPS) that the viewer's files and WebSocket accept. The WebSocket also requires a
  same-site `Origin`.
- `ENABLE_DOCS` serves Swagger UI, ReDoc and the OpenAPI schema behind HTTP Basic auth.
- `TRUSTED_PROXY_IPS` lists reverse proxies whose `X-Forwarded-For` identifies the client.
- Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: SAMEORIGIN` and `Content-Security-Policy: frame-ancestors 'self'`.
- Error code `VNC_UNAVAILABLE` (`502`) when the in-container noVNC server does not answer.
- One `Render timing` log line per render request with the duration of each phase (host slot,
  HTTP attempt, browser queue, restart, context, navigation, readiness, page read, output), the
  total, the trace id, the outcome, why the readiness wait ended (`readiness_end`) and how many
  polls saw a challenge (`challenge_polls`). It replaces the former `Rendered ... in ...s` line
  and is also written for failed requests.
- Clearance reuse: anti-bot clearance cookies from an allow-list (`aws-waf-token`, `cf_clearance`,
  `datadome`, `reese84`, `incap_ses_*`, `visid_incap_*`, `_px3`, `_pxvid`, `_abck`, `bm_sz`)
  earned by a browser render are set in the next render's fresh context for the same site and
  egress route, so a solved challenge is not re-run on every request. Configured with
  `CLEARANCE_REUSE` (default `true`) and `CLEARANCE_MAX_AGE_SECONDS` (default `240`). The render
  timing line gains `clearance=reused|stored|dropped|none`, and `/health/detail` reports the
  number of stored clearance cookies. See ADR 0003.
- `wait_for_settle` (with `wait_for`, 0-10 s, default 3) sets how long the `wait_for` element
  must stay present on the loaded page before a page that never settles is returned with
  `X-Render-Stable: false`. `0` returns as soon as the element is there. Browser only; the
  default behaviour is unchanged.

### Changed

- The browser engine changed from the previous browser driver to zendriver. Every request uses a
  fresh, isolated browser context; only allow-listed clearance cookies are carried over (see
  Added).
- Readiness is now decided by `readyState`, DOM stability, the `wait_for` element and challenge
  detection.
- The image runs as a non-root user with tini, desktop fonts and a pinned base image. The compose
  file runs it read-only with dropped capabilities and resource limits.
- Log timestamps are UTC in ISO 8601.

### Fixed

- Generic anti-bot interstitials are now recognised as challenges, in the browser and on the
  HTTP fast path. Before, a page such as an AWS WAF challenge with a localised title ("Ich bin
  kein Roboter") matched no vendor rule and could be returned as a stable, complete page.
  Detection now covers human-verification phrases in the title or `h1`/`h2` (German, English,
  French, Spanish) and the challenge SDKs of AWS WAF, hCaptcha, Cloudflare Turnstile and
  reCAPTCHA, both only on pages with under 1000 characters of visible text so that normal pages
  embedding these SDKs are unaffected. "Verify you are human" in a title therefore now also
  needs a small page; Cloudflare's own page is still caught by its markers.

## [1.x]

The original renderer, before this changelog was introduced.
