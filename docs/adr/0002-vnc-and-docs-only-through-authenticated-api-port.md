# ADR 0002: VNC and API docs served only through the authenticated API port

- Status: accepted
- Date: 2026-09-30

## Context

The service runs on a public VPS. TLS terminates at a reverse proxy in front of port 8000; the
service itself speaks plain HTTP. Three interactive surfaces were exposed in ways that did not
fit that setup:

- `/vnc` framed the noVNC viewer at `host:6080`. `docker-compose.yml` published 6080 on
  localhost only, so the page was blank on the public domain unless an SSH tunnel existed.
- The alternative, `NOVNC_PREFIX`, proxied noVNC's files and WebSocket under `/novnc` **without
  authentication** and swallowed every relay error. Only the VNC password stood between the
  internet and a live view of the browsers.
- `/docs`, `/redoc` and `/openapi.json` were public and described every endpoint.

Nothing limited how many API keys a client could try.

## Decision

1. **One published port.** websockify binds to `127.0.0.1` inside the container; port 6080 is
   neither published nor exposed. `NOVNC_PREFIX` and `/novnc` are removed.
2. **The live view lives under `/vnc` and is authenticated end to end.**
   - `GET /vnc` requires HTTP Basic (any username, password = an API key). It returns a page that
     frames `/vnc/app/vnc.html` by a path relative to the origin, so it works on any domain.
   - Browsers do not reliably send Basic credentials on WebSocket upgrades. After the Basic
     login, `GET /vnc` therefore sets a session cookie: `<expiry>.<HMAC-SHA256>`, keyed with 32
     random bytes generated per process, valid 8 hours, `HttpOnly`, `SameSite=Strict`,
     `Path=/vnc`, and `Secure` when `X-Forwarded-Proto` (or the request) says HTTPS.
   - `GET /vnc/app/{path}` and `WS /vnc/websockify` accept that cookie or Basic credentials.
     Static paths pass an allow-list (plain segments, no dot segments, no escapes); only `GET`
     is routed. The WebSocket additionally requires an `Origin` that matches `Host` or
     `X-Forwarded-Host`, which blocks cross-site WebSocket hijacking.
   - Upstream failures map to `502 VNC_UNAVAILABLE`; relay tasks end quietly on normal
     disconnects and log anything else at ERROR.
   - With `ENABLE_VNC=false` the routes are not mounted and answer `404`.
3. **Docs are opt-in and authenticated.** FastAPI's built-in documentation routes are disabled.
   With `ENABLE_DOCS=true`, guarded replacements (same Basic scheme) serve Swagger UI, ReDoc,
   the OAuth2 redirect page and the schema; otherwise they answer `404`.
4. **Failed-login lockout.** Wrong Bearer or Basic keys are counted per client address in a
   sliding window. After 10 within 5 minutes, further wrong keys from that address get
   `429 TOO_MANY_AUTH_FAILURES` with `Retry-After` instead of `401`. A valid key always passes
   and is not counted: the lockout must never lock out legitimate clients that share an address
   with an attacker. Guessing a key is infeasible anyway, because the configuration requires
   keys of at least 32 characters. The state is in memory and capped at 10,000 addresses.
   The address is the TCP peer; `X-Forwarded-For` counts only when the peer is in
   `TRUSTED_PROXY_IPS`, read from the right, so clients cannot choose their own address.
5. **Security headers on every response:** `X-Content-Type-Options: nosniff`,
   `Referrer-Policy: no-referrer`, `X-Frame-Options: SAMEORIGIN` and
   `Content-Security-Policy: frame-ancestors 'self'`. The CSP restricts framing only, so the
   `/vnc` page can frame its own viewer and Swagger UI can still load its CDN assets.

## Consequences

- The live view works at `https://<domain>/vnc` with no tunnel. The reverse proxy must forward
  WebSocket upgrades and preserve `Host` (or set `X-Forwarded-Host`).
- A restart invalidates all VNC sessions; users log in again through the browser's Basic dialog.
- Lockout counters and the session key are per process. The image runs one uvicorn worker;
  several workers would need shared state.
- Behind a reverse proxy without `TRUSTED_PROXY_IPS`, all clients share one lockout. Because a
  valid key always passes, an attacker can only make other clients' wrong keys answer `429`
  instead of `401`; valid clients are never locked out.
- Transport security still depends on the TLS-terminating proxy.
