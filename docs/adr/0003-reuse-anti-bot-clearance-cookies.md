# ADR 0003: Reuse anti-bot clearance cookies between browser renders

- Status: accepted
- Date: 2026-09-30

## Context

Every browser render runs in a fresh browser context (ADR 0001), so it starts without cookies.
A site behind a bot challenge (AWS WAF, Cloudflare, DataDome, Imperva, HUMAN/PerimeterX, Akamai)
therefore challenges every single render. The challenge JavaScript runs, sets a clearance cookie
(for AWS WAF `aws-waf-token`, immune for typically 300 s) and reloads the page. That costs about
2-6 s per render, and on some sites it is the larger part of the render time. The token that
would have avoided the next challenge is thrown away with the context.

## Decision

1. **Allow-list.** Only cookies named in `CLEARANCE_ALLOW_LIST` (`app/scraping/clearance.py`)
   are ever kept: `aws-waf-token`, `cf_clearance`, `datadome`, `reese84`, `_px3`, `_pxvid`,
   `_abck`, `bm_sz`, plus the prefixes `incap_ses_` and `visid_incap_`. Each is set by the
   vendor's bot-management layer to prove a passed check. Session, login, consent and tracking
   cookies are never stored. The list is generic; there are no per-site entries.
2. **Store.** `ClearanceStore` keeps the cookies in memory, keyed by egress route (`direct` or
   `home-proxy`) and cookie domain. An entry lives until the cookie's own expiry or
   `CLEARANCE_MAX_AGE_SECONDS` (default 240 s, below the typical 300 s immunity), whichever comes
   first. The store holds at most 512 entries and evicts the oldest. Its methods never await, so
   concurrent requests on the event loop cannot interleave inside them.
3. **Inject.** After the per-request context is created and before navigation, the stored
   cookies whose domain matches the target host are set in that context with CDP
   `Storage.setCookies` and an explicit `browserContextId`. Domain, path, secure, httpOnly,
   sameSite and expiry are preserved; a host-only cookie is set through a URL so it stays
   host-only. A domain cookie matches the domain and its subdomains, never a lookalike such as
   `evil-example.com`.
4. **Harvest.** After a render that ended without a challenge and with a status in 200-399, the
   context's cookies are read with `Storage.getCookies`, filtered by the allow-list and stored.
   A render that ends on an unresolved challenge (`TARGET_BLOCKED`) stores nothing, because its
   token is not proven valid. If that render had injected stored cookies, they are dropped for
   that route and host, so the next render starts clean. Partitioned cookies are skipped.
5. **Never fatal.** A CDP failure while injecting or reading is logged at WARNING and the render
   continues without reuse. Cookie values never appear in logs or responses; `/health/detail`
   reports only the number of entries.
6. **Visible.** The render timing line carries `clearance=reused|stored|dropped|none`.
7. **Browser only.** The HTTP fast path and the verifier never send clearance cookies.
8. `CLEARANCE_REUSE=false` switches all of this off.

## Consequences

- Renders of a challenged site after the first one skip the challenge and its reload while the
  token is valid.
- **Tokens are shared across client apps.** All client apps of the service use the same store.
  Clearance cookies carry no user identity or login state, only the proof that this egress IP
  and browser passed a check, so sharing leaks nothing about another app's requests. It does mean
  that one app's traffic can spend a vendor's per-token request budget that another app earned.
- **Detectability.** A real browser keeps its clearance cookie. A client that is challenged from
  the same IP on every page and solves the challenge each time is a stronger bot signal than one
  that presents its token. Reuse therefore reduces, not adds, detection surface.
- **Binding.** Vendors bind tokens to the client IP and often to the browser fingerprint. The
  store separates egress routes for the IP. All workers run the same Chrome build with the same
  launch flags, locale and timezone, so the fingerprint a token was issued to is the one that
  presents it. A token the vendor rejects anyway ends in a challenge that is solved as before,
  and the rejected token is dropped when the challenge stays unresolved.
- Renders of a challenged site are no longer fully independent of earlier renders.
  All other state (storage, cache, other cookies) is still isolated per request.
- The store is lost on restart; the first render afterwards pays the challenge once.

## Alternatives considered

- **A persistent browser profile per worker.** Would keep clearance cookies, but also every
  other cookie, local storage and cache, and would mix the state of all client apps and both
  egress routes. It would break the per-request isolation that ADR 0001 relies on.
- **Sending the token over the HTTP fast path.** curl_cffi presents a different TLS and
  User-Agent fingerprint than the Chrome that earned the token, so the vendor may reject it or
  flag the mismatch. Possible follow-up once the fingerprints are shown to match.
