# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [2.0.0] - Unreleased

### BREAKING CHANGES

- `POST /render` was removed. Use `POST /api/v1/render`; the request body is unchanged.
- The error body is now `{"error": {"code", "message", "traceId"}}` with `UPPER_SNAKE_CASE` codes.
  It used to be `{"detail": {"error", "detail"}}`.
- Validation errors return `400 VALIDATION_ERROR` with per-field details instead of `422`.
- When capacity is exhausted the service returns `429 SERVICE_BUSY` with `Retry-After`. It used
  to return `503 pool_exhausted` immediately. Requests now wait in a queue first.
- Unknown request fields are rejected. `timeout` must be between 5 and 120.
- Images and CSS are no longer blocked by default. Use `block_resources: true` per request.
  `BLOCK_IMAGES_AND_CSS` and `WAIT_FOR_COMPLETE_PAGE_LOAD` were removed.
- The noVNC view is off by default. It requires `ENABLE_VNC=true` and `VNC_PASSWORD`, and
  `docker-compose.yml` publishes it on localhost only.
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

### Changed

- The browser engine changed from botasaurus-driver to zendriver. Every request uses a fresh,
  isolated browser context.
- Readiness is now decided by `readyState`, DOM stability, the `wait_for` element and challenge
  detection.
- The image runs as a non-root user with tini, desktop fonts and a pinned base image. The compose
  file runs it read-only with dropped capabilities and resource limits.
- Log timestamps are UTC in ISO 8601.

## [1.x]

The botasaurus-based renderer, before this changelog was introduced.
