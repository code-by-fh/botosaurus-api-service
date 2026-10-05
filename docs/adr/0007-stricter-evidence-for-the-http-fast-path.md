# ADR 0007: Stricter evidence for the HTTP fast path

- Status: accepted
- Date: 2026-10-01
- Amends: ADR 0001 (decision 2), ADR 0006 (late-content observation)

## Context

ADR 0001 unlocks the HTTP fast path for a site section once `VERDICT_MIN_SAMPLES` different
URLs passed a comparison of a browser render with an HTTP fetch. A review found four ways in
which incomplete evidence could unlock it, or complete evidence could never arrive:

1. The verification fetch started right after the browser render, 1 to 3 s later. The
   late-content watch (ADR 0006) needs about 10 s to show that the render was incomplete, is
   skipped when requests are queued, can be cut short, and is off with
   `LATE_CONTENT_OBSERVE_SECONDS=0`. A match against an early, incomplete render could therefore
   be recorded before, or without, the watch that would have disproved it.
2. "Different URLs" compared host plus raw path. `/p/a`, `/p/A`, `/p/%61`, `//p/a` and
   `/p/a;jsessionid=X` counted as different pages, so one page could unlock a section alone.
3. Numbers were matched as `\d+` tokens and compared as sets. The browser's `1.299,99` became
   `{1, 299, 99}`, which an HTTP page saying "ab 99 Cent, 1 Jahr, 299 Bewertungen" contained.
4. A section that only ever has one page (a homepage: depth 0) could never collect two different
   URLs, so `https://quotes.toscrape.com/` was always rendered in the browser.

## Decision

**A browser render is a verification reference only after a clean late-content watch.** The
verification is started by `SectionLearning.observed` when a completed, not stopped watch saw no
growth, instead of by the scraper after the render. A skipped, stopped or failed watch, or a
disabled one, starts no verification from that render. A watch that could not read the page once
during its window (every observation hit a replaced document) counts as failed, not as clean.
A section without a verdict is watched on every render (still only while no request waits for a
browser), not only on its first two and on sampled ones, so it keeps collecting evidence.

**Page identity is normalised for counting, and identical text counts once.** The identity of a
match is the host plus the path with percent-escaped unreserved characters decoded, `;` path
parameters and empty segments dropped, and the path lower-cased. Lower-casing can only merge
pages that a case-sensitive server tells apart, so it can only withhold a verdict. Each match
also stores a 16-character SHA-256 prefix of the normalised browser text. Matches that share an
identity or a text hash are joined into one page, which also covers different URLs that redirect
to the same page. At most `MAX_MATCHES_PER_SECTION` (32) matches are kept per section; later
ones are ignored, which can only withhold a verdict.

**Numbers keep their separators and are counted.** A number is a run of digit groups joined by
`.`, `,`, apostrophes, no-break or thin spaces, normalised by removing the separators
(`1.299,99` becomes `129999`). Every browser number must occur in the HTTP text at least as often
as in the browser text (multiset comparison).

**One page can earn a verdict over time.** A section also becomes `HTTP_SUFFICIENT` when the
same page identity matched in `VERDICT_MIN_SAMPLES` verifications that are at least
`VERDICT_SAME_PAGE_INTERVAL_SECONDS` (default 600, at least 60, below `VERDICT_TTL_SECONDS`)
apart: evidence that the page is stably server-rendered, not one lucky fetch. A page that matched
within the interval is not fetched again (`VerdictStore.wants_sample`), because the match would
not count. One mismatch still marks the section browser-only.

## Consequences

- With `LATE_CONTENT_OBSERVE_SECONDS=0` no section ever becomes `HTTP_SUFFICIENT`; every request
  is rendered in the browser.
- On an idle service every render of an unverified section holds its worker for the watch
  (default 10 s) after the response. Under load nothing is watched and nothing is verified, so a
  busy service learns verdicts more slowly.
- A homepage-like section needs about `VERDICT_SAME_PAGE_INTERVAL_SECONDS` (10 min by default)
  of occasional traffic before it is served over HTTP.
- Sites that serve different pages under paths differing only in case, or whose different pages
  show identical text, need more distinct pages before they are trusted.
