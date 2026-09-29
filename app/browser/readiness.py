"""Decides when a page in the browser is fully rendered.

``load`` alone is not enough for JavaScript-heavy pages: content keeps arriving
after it. A page counts as ready when, at the same time,

- ``document.readyState`` is ``complete`` (or the load budget is used up),
- no anti-bot challenge is shown,
- the ``wait_for`` element (if any) exists, and
- the amount of visible text and the number of DOM nodes have not changed for
  ``STABLE_POLLS`` consecutive polls; until the load budget is used up, the
  number of finished XHR/fetch requests must not change either.

The network part is dropped after the load budget because pages that poll or
send analytics beacons would otherwise never count as settled.

Two shortcuts avoid waiting for the full timeout:

- The ``wait_for`` element is present on a loaded page, but the DOM never
  settles (carousels, tickers): the page is returned ``found_settle_seconds``
  after the element appeared, marked as not stable. Without ``wait_for`` there
  is no such shortcut, since a changing page may still be rendering.
- Only when the caller sets ``idle_give_up_seconds``: the ``wait_for`` element
  is missing, but the loaded page has been completely idle (no DOM change and
  no finished XHR/fetch) for that long, so the wait ends early with a timeout
  error. This is opt-in because an idle page does not prove that nothing is
  coming: content scheduled by a timer or pushed over a websocket arrives
  without any prior activity, and a pending timer cannot be observed without
  patching the page (itself a bot signal).
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

from zendriver.core.connection import ProtocolException

from app.content.completeness import CHALLENGE_BODY_MARKERS, CHALLENGE_TITLE_PATTERN
from app.errors import RenderTimeoutError, TargetBlockedError

POLL_INTERVAL_SECONDS = 0.3
STABLE_POLLS = 4
LOAD_BUDGET_SHARE = 0.6

PROBE_SCRIPT_TEMPLATE = """(() => {
  const selector = %(selector)s;
  const markers = %(markers)s;
  const titlePattern = new RegExp(%(title_pattern)s, "i");
  const root = document.documentElement;
  const html = root ? root.outerHTML.toLowerCase() : "";
  let found = true;
  if (selector !== null) {
    try { found = document.querySelector(selector) !== null; } catch (e) { found = false; }
  }
  return {
    ready: document.readyState,
    textLength: document.body ? document.body.innerText.length : 0,
    nodeCount: document.getElementsByTagName("*").length,
    requestCount: performance.getEntriesByType("resource").filter(
      (entry) => entry.initiatorType === "xmlhttprequest" || entry.initiatorType === "fetch"
    ).length,
    challenge: titlePattern.test(document.title) || markers.some((m) => html.includes(m)),
    found: found,
  };
})()"""


class EvaluatingTab(Protocol):
    """The part of a browser tab the readiness check needs."""

    async def evaluate(self, expression: str) -> Any: ...


@dataclass(frozen=True)
class ReadinessTimings:
    """Time windows for the early exits (seconds)."""

    found_settle_seconds: float = 3.0
    idle_give_up_seconds: float | None = None


DEFAULT_TIMINGS = ReadinessTimings()


@dataclass(frozen=True)
class WaitTarget:
    """What to wait for and for how long."""

    wait_for: str | None
    budget_seconds: float


@dataclass(frozen=True)
class PageProbe:
    """One observation of the page state."""

    ready: bool
    text_length: int
    node_count: int
    request_count: int
    challenge: bool
    found: bool

    def same_content_as(self, other: "PageProbe", include_network: bool) -> bool:
        same_dom = (self.text_length, self.node_count) == (other.text_length, other.node_count)
        return same_dom and (not include_network or self.request_count == other.request_count)


NOT_READY = PageProbe(False, 0, 0, 0, False, False)


def build_probe_script(wait_for: str | None) -> str:
    """Return the JavaScript probe; all values are JSON-encoded, never interpolated raw."""
    return PROBE_SCRIPT_TEMPLATE % {
        "selector": json.dumps(wait_for),
        "markers": json.dumps(list(CHALLENGE_BODY_MARKERS)),
        "title_pattern": json.dumps(CHALLENGE_TITLE_PATTERN.pattern),
    }


async def probe(tab: EvaluatingTab, script: str) -> PageProbe:
    """Run the probe script in ``tab`` and convert the result.

    While a navigation replaces the document (redirects, a solved challenge
    reloading the page) the evaluation fails; that poll then counts as not ready.
    """
    try:
        raw = await tab.evaluate(script) or {}
    except ProtocolException:
        return NOT_READY
    return PageProbe(
        ready=raw.get("ready") == "complete",
        text_length=int(raw.get("textLength", 0)),
        node_count=int(raw.get("nodeCount", 0)),
        request_count=int(raw.get("requestCount", 0)),
        challenge=bool(raw.get("challenge", False)),
        found=bool(raw.get("found", False)),
    )


class _Progress:
    """Change tracking across polls."""

    def __init__(self, now: float):
        self.last: PageProbe | None = None
        self.unchanged_polls = 0
        self.idle_since = now
        self.found_since: float | None = None

    def update(self, current: PageProbe, now: float, include_network: bool) -> None:
        settled = self.last is not None and current.same_content_as(self.last, include_network)
        self.unchanged_polls = self.unchanged_polls + 1 if settled else 0
        idle = self.last is not None and current.same_content_as(self.last, True)
        if not idle or current.challenge:
            self.idle_since = now
        usable = current.found and current.ready and not current.challenge
        if not usable:
            self.found_since = None
        elif self.found_since is None:
            self.found_since = now
        self.last = current


class ReadinessWaiter:
    """Polls a tab until it is ready or the time budget is spent."""

    def __init__(
        self,
        tab: EvaluatingTab,
        target: WaitTarget,
        timings: ReadinessTimings = DEFAULT_TIMINGS,
    ):
        self._tab = tab
        self._target = target
        self._timings = timings
        self._script = build_probe_script(target.wait_for)

    async def wait(self) -> bool:
        """Wait for readiness.

        :return: ``True`` if the page became stable, ``False`` if it is returned
            while the content was still changing (``wait_for`` present but the
            DOM never settled, or the budget ran out).
        :raises TargetBlockedError: if a challenge is still shown at the deadline.
        :raises RenderTimeoutError: if ``wait_for`` never appeared, either at the
            deadline or once the loaded page stayed idle without it.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        load_deadline = start + self._target.budget_seconds * LOAD_BUDGET_SHARE
        deadline = start + self._target.budget_seconds
        progress = _Progress(start)
        while True:
            current = await probe(self._tab, self._script)
            now = loop.time()
            load_budget_spent = now >= load_deadline
            progress.update(current, now, include_network=not load_budget_spent)
            outcome = self._decide(current, progress, now, load_budget_spent)
            if outcome is not None:
                return outcome
            if now >= deadline:
                return self._at_deadline(current)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _decide(
        self, current: PageProbe, progress: _Progress, now: float, load_budget_spent: bool
    ) -> bool | None:
        loaded = current.ready or load_budget_spent
        settled = progress.unchanged_polls >= STABLE_POLLS - 1
        if loaded and settled and current.found and not current.challenge:
            return True
        if self._target.wait_for and self._found_long_enough(progress, now):
            return False
        if self._element_is_not_coming(current, progress, now):
            raise RenderTimeoutError(
                f"'{self._target.wait_for}' did not appear; the page finished loading and "
                f"stayed idle for {self._timings.idle_give_up_seconds:.0f}s"
            )
        return None

    def _found_long_enough(self, progress: _Progress, now: float) -> bool:
        # Only with wait_for: the caller named an element that proves the
        # content is there. Without it, a still-changing page may still be
        # rendering and must not be cut short.
        if progress.found_since is None:
            return False
        return now - progress.found_since >= self._timings.found_settle_seconds

    def _element_is_not_coming(self, current: PageProbe, progress: _Progress, now: float) -> bool:
        give_up_after = self._timings.idle_give_up_seconds
        if give_up_after is None:
            return False
        waiting_for_element = current.ready and not current.found and not current.challenge
        return waiting_for_element and now - progress.idle_since >= give_up_after

    def _at_deadline(self, current: PageProbe) -> bool:
        if current.challenge:
            raise TargetBlockedError("The target kept showing an anti-bot challenge")
        if not current.found:
            raise RenderTimeoutError(
                f"Timed out after {self._target.budget_seconds:.0f}s "
                f"waiting for '{self._target.wait_for}'"
            )
        return False
