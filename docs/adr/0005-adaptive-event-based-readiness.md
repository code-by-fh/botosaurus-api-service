# ADR 0005: Adaptive, event-based readiness

- Status: accepted, extended by ADR 0006 (learned readiness floors)
- Date: 2026-10-01
- Amends: ADR 0004 (trackers are ignored by readiness instead of blocked)

## Context

The service must never return a half-rendered page, and it should return as soon as the page
is complete. The readiness check before this decision polled the page every 300 ms and counted
it as rendered after `STABLE_POLLS = 4` identical probes of visible text length, DOM node count
and the number of *finished* XHR/fetch entries in `performance.getEntriesByType("resource")`.
Its weaknesses:

1. **In-flight requests were invisible.** Only finished entries appear in the Resource Timing
   buffer, so a slow API call looked exactly like no call.
2. **The buffer freezes.** It holds 250 entries by default; on a page with more resources the
   counter stopped changing and the network signal was silently lost.
3. **Trackers kept pages busy.** Analytics beacons and pings finish all the time, so most
   renders never saw four identical probes before the load budget (60% of `timeout`) and ended
   with `readiness_end=load-budget-expired`, many seconds later than necessary.
4. **A fixed floor.** Four probes 300 ms apart cost at least 1.2 s on every page, also on pages
   that were complete on arrival.
5. **Length is not content.** A text swapped for one of equal length (a price replacing a
   placeholder of the same width) left both numbers unchanged.

How others decide:

- **prerender.io** counts in-flight requests through the DevTools protocol
  (`Network.requestWillBeSent` / `loadingFinished` / `loadingFailed`) and treats a page as done
  about 500 ms after the last request ended.
- **Jina Reader** watches content resource classes and debounces a `MutationObserver` for about
  200 ms.
- **Blink's `IdlenessDetector`** (used for `networkIdle` lifecycle events) waits for a quiet
  network window of 500 ms with at most 0 or 2 open connections.
- **Playwright** discourages `networkidle` for tests: background traffic makes it either never
  fire or fire too late, and an idle network says nothing about the DOM.

## Decision

A page counts as ready when, at the same time:

1. The document is loaded: `readyState` is `complete`, or `interactive` for
   `LOAD_WATCHDOG_SECONDS` (2 s), or the load budget is spent.
2. No challenge is shown (definitions from `content/completeness.py`, evaluated in the page as
   before) and the `wait_for` element exists.
3. No visible loading placeholder is on the first screen: `[aria-busy="true"]`, an element whose
   class or id has `skeleton`, `spinner`, `loader`, `loading` or `placeholder-shimmer` as its
   own token (so `file-uploader` and `lazyloading` do not match), or a short text node that only
   says "Loading..." (English, German, French, Spanish, Italian). One named definition each in
   `readiness.py`. Placeholders below the first screen are ignored: content that loads on
   scroll never arrives without scrolling, and an infinite-scroll spinner would otherwise hold
   every such page to the deadline.
4. No content request is in flight (`network_activity.InflightTracker`). The tracker listens to
   `Network.requestWillBeSent`, `responseReceived`, `loadingFinished` and `loadingFailed` on the
   tab. It counts only documents (main frame and iframes), scripts, XHR and fetch, and ignores
   pings, event streams (from their response on), WebSockets, images, fonts, media,
   stylesheets, prefetches and `Other`; every host on the shipped tracker list
   (`default_tracker_domains()`, domain and subdomains, minus the domain the page is on); and
   any request open for `LONG_POLL_SECONDS` (5 s), which is treated as a long poll.
5. Neither a content request started or ended nor did the DOM grow for the **quiet window**.

**DOM growth** is observed by a `MutationObserver` (childList, subtree, characterData) installed
in an isolated world (`Page.createIsolatedWorld` on the main frame, then `Runtime.evaluate` with
its `contextId`). It counts a mutation as growth only when it adds a text node whose text was
not seen before in this document; a bounded set of text hashes (20 000) makes a carousel that
rotates known texts count as churn. Attribute-only mutations are ignored. Each poll evaluates
one script in that world that installs the observer if needed and returns the growth counter,
`readyState`, challenge, `wait_for` and placeholder state. The world dies with its document; a
failed evaluation after a redirect or a solved challenge's reload creates a new world on the
next poll, and the new world counts as growth. If the world cannot be created, the same script
runs in the main world without the observer and reports a text-length/node-count fingerprint,
as the old probe did; a warning is logged once per render.

**The quiet window adapts.** It starts at `BASE_QUIET_SECONDS` (0.5 s) and grows to
`QUIET_GAP_FACTOR` (2) times the longest idle gap seen between two content events of this
render (network starts and ends and DOM growth, on one timeline), capped at
`MAX_QUIET_SECONDS` (3 s). Only gaps without an open content request count: waiting for a slow
API response is not rhythm. Once the `wait_for` element is present, the window is capped at
`WAIT_FOR_QUIET_CAP_SECONDS` (1 s), because the element proves the content is there. At least
two consecutive identical observations are required, so a zero window cannot end the wait on
its first poll.

**The page is polled every 100 ms** (`POLL_INTERVAL_SECONDS`).

**The load budget stays as an emergency brake.** The tracker and long-poll exclusions remove
the background traffic that made the old rule necessary, but a page that fetches short-lived
JSON every second (live prices, tickers) would never be network-quiet. After 60% of the timeout,
network activity is ignored and only DOM growth counts (`load-budget-expired`).

**Unchanged:** at the deadline a page is returned with `stable=false` (`200`,
`X-Render-Stable: false`), a challenge still shown raises `TARGET_BLOCKED`, a missing `wait_for`
element raises `TIMEOUT`.

**The service decides; the API carries no timing knobs.** The per-request fields
`wait_for_settle` and `idle_timeout` of the unreleased 2.0.0 drafts were removed; unknown fields
are rejected with `400`, so callers that still send them get a clear field error.

- The restless-page shortcut stays: once the `wait_for` element has been present on a loaded,
  unchallenged page for `WAIT_FOR_QUIET_CAP_SECONDS`, the page is returned with
  `stable=false` (`wait-for-found`). The same internal constant caps the quiet window, so a
  caller cannot trade completeness for speed and needs no knowledge of the page's rhythm.
- There is no early give-up for a missing element. A quiet page does not prove that nothing is
  coming: `quotes.toscrape.com/js-delayed/` is silent for about 10 s before its content
  appears, and a pending timer cannot be observed without patching the page. A missing element
  is awaited until `timeout`, which is an upper bound, not a target.

**Observability:** the `ReadinessEnd` value is sent as `X-Render-Ready-Reason` (`verified-http`
for the HTTP fast path), and the timing line gains `quiet_ms` and `inflight_ignored`.

## Consequences

- Pages that are complete on arrival return after about 0.5 s of readiness instead of 1.2 s or
  more; pages with analytics no longer run to the load budget.
- In-flight content requests now hold the page back, also slow ones, and the 250-entry buffer
  limit no longer matters. Equal-length text swaps count as growth.
- **Detectability.** `Network.enable` (sent by zendriver when the first network handler is
  registered) and isolated worlds are invisible to page scripts; the observer has no global in
  the page's world and patches nothing. `Runtime.enable`, a known CDP detection signal, is still
  never sent: zendriver 0.17.0 does not send it on its own, and every evaluation uses
  `Runtime.evaluate` only. zendriver's `tab.evaluate` is not used, because it sends
  `userGesture: true`, which the page can observe (`navigator.userActivation.hasBeenActive`) and
  which grants popup and autoplay activation; `browser/evaluation.py` sends `userGesture: false`.
  The main frame id is read with a raw `Page.getFrameTree` command, because zendriver's
  `FrameTree` parser breaks on fields newer Chrome versions add.
- **Residual risk: silent timers.** A page that inserts content from a `setTimeout` after more
  than the quiet window of complete silence (no request, no DOM change) can be returned before
  that content exists. A pending timer cannot be observed without patching the page.
  Per-section learning of render durations (ADR 0006) addresses this from the first render
  after a finished late-content observation; before that, `wait_for` is the remedy.
- **Residual risk: out-of-process iframes.** Requests of cross-site iframes run in another
  renderer and are not reported on the page's session, so they do not hold the page back. The
  main document's DOM growth and `readyState` still do.
- **Residual risk: Shadow DOM and same-origin iframes.** The `MutationObserver` watches the main
  document only, and the placeholder check queries it only. Content that a page renders into a
  shadow root or a same-origin iframe does not count as growth, and a skeleton inside one does not
  hold the page back. Such a page can be returned before that content exists; `wait_for` on an
  element of the main document that appears with it is the remedy.
- The deadline decides on the last observation that succeeded, not on the last poll: a poll that
  fails because the document is being replaced cannot turn a challenge into a timeout or report a
  missing element for a request without `wait_for`. If no observation ever succeeded, the render
  fails with `NAVIGATION_FAILED`; three failures in a row that report a closed or crashed target
  fail it at once.
- In the main-world fallback no late-content watch runs (ADR 0006): its growth token, text length
  and node count, changes with every carousel rotation and would mark sections browser-only.
- A request without a resource type counts as content; a lost `loadingFinished` event can hold
  a page back for at most `LONG_POLL_SECONDS`.
- Placeholder detection only lengthens waits. A placeholder that never disappears from the first
  screen holds the page until the deadline, where it is returned with `X-Render-Stable: false`.
- Integration tests run on the real clock with the base window patched to 0; the readiness unit
  tests run on a manual clock with the production constants. Behaviour against a real Chrome is
  not covered by the test suite.
