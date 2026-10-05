# ADR 0008: Automatic proxy escalation on bot block

- Status: accepted
- Date: 2026-10-05
- Amends: ADR 0001 (egress routes), ADR 0005 (challenge handling in readiness)

## Context

The service runs on a VPS whose address belongs to a well-known datacenter range. Strong bot
protection (Cloudflare Bot Management, DataDome, Akamai, ...) challenges such addresses whatever
the browser fingerprint, and the challenge never resolves. Until now the only remedy was
`use_proxy: true`, which routes a request through `HOME_PROXY` (a residential or home exit). That
put the burden on every client app: it had to know which sites block the VPS, and a blocked
request first waited its whole `timeout` (30 s by default) on a challenge that was never going to
resolve before it failed with `502 TARGET_BLOCKED`, after which the client had to retry it with
`use_proxy`.

## Decision

1. **Escalate on block.** When `HOME_PROXY` is set, `AUTO_PROXY_ON_BLOCK` is true (default) and
   the request did not set `use_proxy`, a direct browser render that ends in `TargetBlockedError`
   is rendered once more with `use_proxy=True`. There is at most one escalation per request.
2. **Give up early.** For such a request the direct job carries `challenge_patience_seconds`
   (`AUTO_PROXY_CHALLENGE_SECONDS`, default 8 s, 2-60). `ReadinessWaiter` raises
   `ChallengePersistedError`, a `TargetBlockedError`, when a challenge is still shown that long
   after a poll first saw it; a poll without a challenge resets the clock. The waiter only gets
   this number and knows nothing about proxies or routes. A challenge that solves itself within
   the patience (most JavaScript checks take 2-6 s) is unaffected. A block at the deadline also
   escalates, if time is left.
3. **Remaining budget only.** The retry gets `timeout` minus everything spent since the host slot
   was acquired. If less than `MIN_READINESS_BUDGET_SECONDS` (1 s) remains, the original
   `502 TARGET_BLOCKED` is returned without a retry. For the same reason no patience is set when
   `timeout` minus the patience is below that minimum: giving up early would only lose the
   chance that the challenge solves itself, so such a request waits the whole `timeout` on the
   direct route as before. If the proxy render is blocked as well, the request fails with
   `502 TARGET_BLOCKED`.
4. **Slots.** The per-host slot is held across both attempts: they are one request to one host,
   and releasing it would let other requests overtake the retry. The browser worker is not held.
   The blocked attempt returns it to the pool, and the retry queues for a worker like any new
   request. Holding a worker while waiting for another (or for the same one) could deadlock with
   `MAX_WORKERS=1` and would bypass the queue limit. The cost is that the retry may wait in the
   queue (not counted in `timeout`, like the first queue wait) or get `429 SERVICE_BUSY` when the
   queue is full.
5. **Remember per host.** A host whose proxy retry succeeded is kept in `ProxyHostStore`
   (`scraping/proxy_hosts.py`) for `AUTO_PROXY_TTL_SECONDS` (default 6 hours, 60-604800), bounded
   to 1024 hosts (oldest dropped), on the injected monotonic clock. Later requests to that host
   without `use_proxy` go through `HOME_PROXY` from the start. A successful direct render does
   not clear an entry; only its expiry does, after which the host is tried directly again. A
   host whose proxy retry was blocked too is not remembered, since the proxy did not help.
6. **No learning from proxy renders.** The escalated render runs without the learning hook: it
   neither verifies its section for the HTTP fast path nor records timing (its timing includes
   the home uplink). Remembered hosts skip the HTTP fast path and are never verified, because
   curl_cffi fetches over the direct route, which is blocked for them; a direct HTTP match must
   not earn `HTTP_SUFFICIENT` for a section the browser can only reach by proxy.
7. **Visible.** Every successful response carries `X-Render-Route: direct|proxy`; the timing log
   line carries `route=` and `escalated=true|false`; `/health/detail` reports
   `proxy_hosts.entries`. Clearance cookies stay per egress route (ADR 0003).

## Consequences

- Clients no longer need to know which sites block the VPS. A blocked request costs about
  `AUTO_PROXY_CHALLENGE_SECONDS` plus a proxy render instead of a full `timeout` and a client
  retry; later requests to the host skip the direct attempt entirely.
- **Home upload bandwidth.** Every escalated and remembered render downloads the full page with
  all its subresources through the home connection, whose upstream is typically far smaller than
  the VPS's. Many blocked hosts can saturate it and slow both the service and the household.
  `AUTO_PROXY_ON_BLOCK=false` restores explicit opt-in.
- **Home IP exposure.** Targets see the home address for these requests, automatically and
  without the client app asking. Rate limits or bans the targets apply hit that address and
  everything else behind it. The home exit must be one whose owner accepts this.
- **Why per host, not per section.** Bot protection is applied at the edge for a whole host (or
  site), not per path; a block on one page predicts blocks on the others. Remembering per host
  makes the second request to any page of the site fast. Per-section memory would pay the blocked
  direct attempt once for every section. Remembering per registrable domain would go further, but
  needs a public-suffix list, and hosts of one domain are often protected differently.
- Requests to a remembered host lose the HTTP fast path for the entry's lifetime, so they are
  slower than they would be if the host's direct route worked again; the TTL bounds that.
- A remembered host whose proxy route later gets blocked too fails with `502 TARGET_BLOCKED`
  without trying the direct route, until its entry expires.
- The memory is per process and lost on restart, like verdicts and profiles.
