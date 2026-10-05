# ADR 0006: Per-section readiness learning

- Status: accepted, amended by ADR 0007
- Date: 2026-10-01
- Extends: ADR 0005 (learned floors on top of the adaptive readiness)

## Context

ADR 0005 made readiness event-based: a page is ready when no content request is in flight and
neither a request nor DOM growth happened for an adaptive quiet window (0.5 to 3 s). One gap
remained. Content that a page inserts from a timer after a period of complete silence longer
than the quiet window is missed on a request without `wait_for`.
`quotes.toscrape.com/js-delayed/` is silent for about 10 s before its quotes appear. A pending
timer cannot be observed without patching the page, which is itself a bot signal. A single
render cannot tell such a page from a finished one.

The request API has no timing knobs (ADR 0005), and callers should not need to know a site's
rhythm. The service serves many client apps from a small VPS (two browser workers by default),
so whatever closes the gap must not cost a worker on most requests.

## Decision

**The service learns per site section how long its pages take.** `SectionProfileStore`
(`app/scraping/profiles.py`) is modelled on the verdict store: the same `section_key`
(host, first path segment, depth), an `OrderedDict` capped at `MAX_PROFILED_SECTIONS` (10 000)
with least-recently-recorded eviction, a TTL (`PROFILE_TTL_SECONDS`, default 6 h, counted from
the section's first render), and an injected clock. Each section keeps the last
`PROFILE_WINDOW_SAMPLES` (20) samples of

- the time of the last DOM growth, relative to the start of the readiness wait (the moment the
  navigation committed), and
- the largest idle gap between two content events of that render.

From these it derives two floors, passed to the browser as `ReadinessHints`:

- `min_ready_seconds` = 90th percentile (nearest rank) of the last-growth times +
  `MIN_READY_MARGIN_SECONDS` (0.5 s). The page cannot be declared ready earlier.
- `quiet_floor_seconds` = `QUIET_GAP_FACTOR` (2) x 90th percentile of the largest gaps, capped
  at `MAX_QUIET_SECONDS` (3 s). The quiet window is at least this long.

Only renders that may teach something are recorded: stable, status 200, without
`block_resources`, the same rule that decides whether a render may serve as reference for the
HTTP verification (`learning.is_usable_reference`). The profile store holds no readiness end
reason: stability already implies `settled` or `load-budget-expired`.

**Learned data may only lengthen waits.** Both floors are lower bounds. The floors never end a
wait; only observations of the current render do. A page that settles later than learned still
waits for its own quiet window. With a found `wait_for` element, the 1 s cap on the quiet window
and on the restless-page shortcut applies only where it is not below the learned floor, and the
shortcut also respects `min_ready_seconds`. A floor beyond the time budget is capped at the
budget, so a stable page is still returned as stable at the deadline. Unknown sections use the
behaviour of ADR 0005 unchanged.

**Late-content observation feeds the profile.** Without it, a silent-timer site returns early
on every render and nothing is ever learned. After the response has been returned, the same tab
is watched for `LATE_CONTENT_OBSERVE_SECONDS` (default 10, `0` disables) with the observer of the
readiness wait, so its isolated world and growth counter continue where the wait stopped
(`browser/late_content.py`, poll every 0.25 s). If the content grew, the time of the last growth
raises that render's sample. A replaced document counts as growth. Late content is also proof
that the returned document was incomplete, so it records a verdict mismatch: the section becomes
browser-only. An HTTP fetch that matched the early render would otherwise be able to make an
incomplete page `HTTP_SUFFICIENT`.

**The watch gates the HTTP verification (ADR 0007).** A render becomes the reference of an HTTP
verification only after its watch completed without being stopped and saw no growth. A section
without a verdict is therefore watched on every render (while no request waits), not only on the
first ones and sampled ones. With `LATE_CONTENT_OBSERVE_SECONDS=0` no render is ever verified, so
no section becomes `HTTP_SUFFICIENT`.

**Cost control.**

- A section is observed for its first `PROFILE_OBSERVE_FIRST_RENDERS` (2) renders whose watch
  finished. After that, a render is observed at the rate `PROFILE_OBSERVE_SAMPLE_RATE`
  (default 0.05, random source injected).
- **The observation holds its worker.** One Chrome renders one page at a time and the watched
  tab lives in it, so releasing the worker early would mean either two contexts per browser or
  a second set of browsers. Holding the worker is simple and is made harmless:
  - an observation starts only when no request is waiting for a worker (`BrowserPool` checks its
    queue when the render returns; otherwise the tab is closed at once);
  - a request that arrives while every worker is busy stops a running observation. The
    observation ends within one poll interval plus a CDP round trip, closes its tab and frees
    the worker. A stopped observation teaches nothing.
- Preemption uses a stop event rather than task cancellation. A task cancelled before its first
  step never runs its `finally` blocks, which would leak the tab and the worker.
- An observation has its own time limit (`observe + LATE_WATCH_GRACE_SECONDS`), because the
  worker's hard deadline covers only the render. The context is always closed afterwards. A
  failed observation is logged at WARNING, flags the browser for restart and never affects the
  response that was already returned.

**Visibility.** Browser renders send `X-Render-Profile: cold|learned`. The HTTP fast path sends
`n/a`, so the header is present on every successful response. The timing line gains
`profile=cold|learned` and `min_ready_ms`. `/health/detail` reports `profiles.entries` and the
pool reports `observing` workers separately from `busy` ones.

## Consequences

- A silent-timer section is rendered correctly from the first render that runs after a finished
  observation, without any caller knob. `js-delayed` returns after about 10.6 s instead of
  0.5 s, with the quotes.
- For normal sites the learned floor is close to the time a cold render needs anyway (last
  growth + 0.5 s against last event + quiet window), so they stay fast. A section whose pages
  vary is held to the slow end of its recent pages (90th percentile).
- On an idle service, the first two renders of each new section hold a worker up to 10 s longer
  after their response. Under load nothing is observed, and a waiting request takes the worker
  back at once.
- **Residual risk: the first render of an unknown silent-timer section can still be early.** So
  can every render that starts before the first observation finished. `wait_for` remains the
  remedy for callers that know the element.
- **Residual risk: profiles are shared by every client.** One client's renders lengthen the
  waits of all clients of that section. The lengthen-only rule limits the harm to slower
  responses, never incomplete ones. A page with steady late updates that still settles in
  between (for example a widget that loads after 5 s) teaches a floor of that length.
- **Residual risk: growth beyond the observation window** (content after more than
  `LATE_CONTENT_OBSERVE_SECONDS` after the return) is never learned.
- **Residual risk: per process, in memory.** Profiles are lost on restart and not shared between
  replicas; each process re-learns its sections.
- The p90 ignores the slowest tenth of the samples. With 10 or more samples, a single late
  observation among quick renders does not raise the floor. Once the floor applies, the renders
  that wait for the late content record it themselves and keep it learned.
