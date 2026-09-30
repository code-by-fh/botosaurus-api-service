# ADR 0001: zendriver as browser engine and a verified HTTP fast path

- Status: accepted
- Date: 2026-09-28

## Context

The service must render arbitrary pages completely and avoid bot detection. Many client apps
use it, and it runs on a small VPS (Hetzner CX-class). Several problems had come up:

- The previous driver releases slowly, and its PyPI builds do not match the published source.
- Reusing one browser forever degrades its performance.
- Cookies were shared across all clients.
- Blocking images and CSS by default is itself a detection signal.

Plain HTTP is roughly 10x cheaper than a browser render. However, it silently returns incomplete
pages for sites that render with JavaScript, and that is not acceptable for callers.

## Decision

1. **Browser engine: zendriver.** It drives real Google Chrome over CDP, without WebDriver, like
   the previous driver and nodriver do. Unlike those it is actively maintained, its source is
   transparent, and it supports a proxy per browser context. Every request gets a fresh browser
   context (ADR 0003 carries allow-listed anti-bot clearance cookies over between contexts). Browsers are recycled after `BROWSER_MAX_PAGES` renders or `BROWSER_MAX_AGE_SECONDS`.
2. **HTTP fast path, gated by evidence rather than heuristics alone.**
   - curl_cffi with Chrome impersonation is used only for site sections (host, first path segment
     and path depth) where `VERDICT_MIN_SAMPLES` different URLs passed a comparison against a
     browser render.
   - A comparison passes when the HTTP document contains at least `MIN_TEXT_COVERAGE` of the
     browser's visible words and text length, and every number the browser showed.
   - Each HTTP response must also pass strict completeness checks, and any doubt falls back to the
     browser.
3. **All egress goes through a local guarding proxy.** Chrome (every context and the browser
   itself, with Chrome's implicit loopback/link-local bypass removed) and curl_cffi connect only
   through it. It rejects non-public destinations for every connection and pins the vetted
   address. `HOME_PROXY` is chained behind it.
4. **Verdicts are held in memory.** The service runs as a single instance. Losing verdicts on a
   restart only costs extra browser renders, so external storage such as Redis is not justified
   yet.

## Consequences

- The first requests to every section are rendered in the browser, and each one triggers one extra
  HTTP request after a short random pause.
- A section with mixed rendering is marked browser-only after the first mismatch. That is
  conservative: it costs performance but never completeness.
- Residual detection risks remain outside the scope of the browser engine: datacenter IP
  reputation, and the software WebGL renderer on a GPU-less VPS. See the README for mitigations.
- Scaling out to several instances would need a shared verdict store (Redis, as key-value cache).
