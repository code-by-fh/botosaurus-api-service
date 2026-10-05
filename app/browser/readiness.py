"""Decides when a page in the browser is fully rendered, as early as that is provable.

A page counts as ready when, at the same time,

- the document is loaded: ``readyState`` is ``complete``, or it has been
  ``interactive`` for ``LOAD_WATCHDOG_SECONDS`` (a subresource that never
  finishes must not hold the render back), or the load budget is used up;
- no anti-bot challenge is shown (``completeness`` defines what counts as one;
  the observer evaluates those definitions in the page);
- the ``wait_for`` element (if any) exists;
- no visible loading placeholder is on screen (``aria-busy``, skeletons,
  spinners, a bare "Loading..." text; see ``LOADING_PLACEHOLDER_NAMES``);
- no content request is in flight (``network_activity.InflightTracker``), and
- neither a content request nor DOM growth happened for the quiet window.

DOM growth is observed by a ``MutationObserver`` that runs in an isolated world
(``Page.createIsolatedWorld``): it shares the DOM with the page but none of its
JavaScript, so the page cannot see it. Only *new* text counts as growth: text a
carousel or ticker rotates back in was seen before and counts as churn. If the
isolated world cannot be created, the observer falls back to evaluating in the
page's main world and compares visible text length and node count between
polls, as the readiness check did before. ``Runtime.enable`` is never sent, and
no evaluation claims a user gesture (``evaluation``). The observer sees the main
document only: content in a Shadow DOM or a same-origin iframe is not growth.

The quiet window adapts to the page: it starts at ``BASE_QUIET_SECONDS`` and
grows to ``QUIET_GAP_FACTOR`` times the largest gap seen between two content
events of this render, capped at ``MAX_QUIET_SECONDS``. A page that loads in
one burst returns half a second after its last request; a page that fetches in
waves is given the time its own rhythm suggests. When the ``wait_for`` element
is present, the window is capped at ``WAIT_FOR_QUIET_CAP_SECONDS``, since the
element proves the content is there.

The load budget (``LOAD_BUDGET_SHARE`` of the time budget) is an emergency
brake: after it, network activity is ignored and only DOM growth counts, so
pages that poll for data in short intervals still finish.

One shortcut avoids waiting for the full timeout: the ``wait_for`` element is
present on a loaded page, but the DOM never stops growing (tickers, live
feeds), so the page is returned ``WAIT_FOR_QUIET_CAP_SECONDS`` after the
element appeared, marked as not stable.

A missing ``wait_for`` element is awaited until the deadline, however quiet the
page is. A quiet page does not prove that nothing is coming: content scheduled
by a timer or pushed over a websocket arrives without any prior activity, and a
pending timer cannot be observed without patching the page (itself a bot
signal).

Learned floors of the site section (``ReadinessHints``, from ``scraping.profiles``)
can only lengthen the wait: the page is not declared ready before
``min_ready_seconds`` after the wait began (capped at the budget), and the quiet
window, also the capped one with a found ``wait_for`` element, is at least
``quiet_floor_seconds``. After the wait, ``content_timing`` reports what the
profile learns from, and ``late_watch`` continues observing the returned page
(not after a wait in the main-world fallback, whose growth token cannot tell late
content from a rotating carousel).

A challenge is awaited until the deadline, unless the caller set
``WaitTarget.challenge_patience_seconds``: then a challenge still shown that long
after an observation first saw it (without an observation in between that showed
none) ends the wait with ``ChallengePersistedError``. The scraper sets it only
when it can retry the render through HOME_PROXY; this module knows nothing about
routes.

The deadline decides on the last observation that succeeded: a poll that failed
because the document was being replaced says nothing about the page. If none
succeeded, the page could not be read at all (``NavigationError``); a tab that
crashed or was closed fails at once instead of at the deadline.

After ``wait`` returns or raises, ``ReadinessWaiter.end`` tells why it stopped,
``challenge_polls`` how many polls saw a challenge and ``quiet_seconds`` the
final quiet window; all are logged per request to explain where time went.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from zendriver import cdp
from zendriver.core.connection import ProtocolException

from app.browser.activity import ActivityLog
from app.browser.evaluation import CdpTab, create_isolated_world, evaluate, evaluation
from app.browser.late_content import GrowthBaseline, LateContentWatch
from app.browser.network_activity import IDLE_NETWORK, NetworkSnapshot
from app.content.completeness import (
    CHALLENGE_BODY_MARKERS,
    CHALLENGE_SDK_MARKERS,
    CHALLENGE_TITLE_PATTERN,
    HUMAN_VERIFICATION_PATTERN,
    INTERSTITIAL_MAX_TEXT_CHARS,
    VERIFICATION_TEXT_TAGS,
)
from app.errors import (
    ChallengePersistedError,
    NavigationError,
    RenderTimeoutError,
    TargetBlockedError,
)

log = logging.getLogger("render.browser")

# Module-level values are read at call time, so tests can shorten them.
POLL_INTERVAL_SECONDS = 0.1
BASE_QUIET_SECONDS = 0.5
MAX_QUIET_SECONDS = 3.0
QUIET_GAP_FACTOR = 2.0
LOAD_WATCHDOG_SECONDS = 2.0
LOAD_BUDGET_SHARE = 0.6
# Once the wait_for element is present it proves the content is there, so neither the
# quiet window nor a page that never settles may hold the render back longer than this.
WAIT_FOR_QUIET_CAP_SECONDS = 1.0

READY_STATE_COMPLETE = "complete"
READY_STATE_INTERACTIVE = "interactive"
# Isolated worlds are named only for DevTools; the page never sees the name.
ISOLATED_WORLD_NAME = "render-readiness"
OBSERVER_STATE_KEY = "__renderReadiness"
# Distinct texts remembered per document; text beyond this always counts as growth,
# which can only lengthen the wait.
MAX_SEEN_TEXTS = 20_000
MAX_PLACEHOLDER_CANDIDATES = 200
NON_CONTENT_TAGS = ("SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE")
# CDP errors of a tab that is gone for good, unlike "Execution context was destroyed"
# or "Cannot find context", which a document swap causes and the next poll recovers from.
TARGET_GONE_MARKERS = (
    "target closed",
    "target crashed",
    "session with given id not found",
    "no target with given id",
)
MAX_TARGET_GONE_FAILURES = 3

LOADING_PLACEHOLDER_ATTRIBUTE_SELECTOR = '[aria-busy="true"]'
LOADING_PLACEHOLDER_NAMES = ("skeleton", "spinner", "loader", "loading", "placeholder-shimmer")
# A placeholder name must stand as its own token of a class or id ("page-loader",
# "is-loading", "Spinner_root__x1"), so "file-uploader" or "lazyloading" do not match.
LOADING_PLACEHOLDER_NAME_PATTERN = re.compile(
    r"(?:^|[\s_-])(?:" + "|".join(LOADING_PLACEHOLDER_NAMES) + r")(?:[\s_-]|$)",
    re.IGNORECASE,
)
# A whole short text node that only says the content is on its way.
LOADING_TEXT_PATTERN = re.compile(
    r"^(?:loading|wird geladen|l(?:ä|ae)dt|chargement|cargando|caricamento)"
    r"(?:\s*(?:\.{3}|…))?$",
    re.IGNORECASE,
)
LOADING_TEXT_MAX_CHARS = 24


def placeholder_candidate_selector() -> str:
    """CSS for elements that may be loading placeholders; the name pattern decides."""
    names = (
        f'[{attribute}*="{name}" i]'
        for name in LOADING_PLACEHOLDER_NAMES
        for attribute in ("class", "id")
    )
    return ",".join((LOADING_PLACEHOLDER_ATTRIBUTE_SELECTOR, *names))


OBSERVER_SCRIPT_TEMPLATE = """(() => {
  const config = %(config)s;
  const loadingText = new RegExp(config.loadingText, "i");
  const placeholderName = new RegExp(config.placeholderName, "i");
  const hashText = (text) => {
    let hash = 0x811c9dc5;
    for (let index = 0; index < text.length; index++) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    return hash >>> 0;
  };
  const textNodes = (root) => {
    if (root.nodeType === Node.TEXT_NODE) return [root];
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    return nodes;
  };
  const isLoadingText = (text) =>
    text.length <= config.loadingTextMaxChars && loadingText.test(text);
  const install = () => {
    const state = { growth: 0, seen: new Set(), loadingNodes: new Set() };
    const remember = (node) => {
      const parent = node.parentElement;
      if (!parent || config.nonContentTags.includes(parent.tagName)) return false;
      const text = node.data.trim();
      if (!text) return false;
      if (isLoadingText(text)) state.loadingNodes.add(node);
      const hash = hashText(text);
      if (state.seen.has(hash)) return false;
      if (state.seen.size < config.maxSeenTexts) state.seen.add(hash);
      return true;
    };
    const grows = (record) => {
      const nodes = record.type === "characterData"
        ? [record.target]
        : Array.from(record.addedNodes).flatMap(textNodes);
      return nodes.map(remember).includes(true);
    };
    textNodes(document).forEach(remember);
    new MutationObserver((records) => {
      if (records.map(grows).includes(true)) state.growth += 1;
    }).observe(document, { childList: true, subtree: true, characterData: true });
    globalThis[config.stateKey] = state;
    return state;
  };
  const shown = (element) => {
    const rect = element.getBoundingClientRect();
    const onScreen = rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0
      && rect.top < innerHeight && rect.left < innerWidth;
    if (!onScreen || !element.checkVisibility) return onScreen;
    return element.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  };
  const isPlaceholder = (element) => element.matches(config.busySelector)
    || placeholderName.test(element.getAttribute("class") || "")
    || placeholderName.test(element.id || "");
  const placeholderShown = (state) => {
    const candidates = Array.from(document.querySelectorAll(config.placeholderSelector))
      .slice(0, config.maxPlaceholderCandidates);
    if (candidates.some((element) => isPlaceholder(element) && shown(element))) return true;
    if (!state) return false;
    for (const node of Array.from(state.loadingNodes)) {
      if (!node.isConnected || !isLoadingText(node.data.trim())) state.loadingNodes.delete(node);
    }
    return Array.from(state.loadingNodes).some(
      (node) => node.parentElement && shown(node.parentElement)
    );
  };
  const signals = config.signals;
  const root = document.documentElement;
  const html = root ? root.outerHTML.toLowerCase() : "";
  const textLength = document.body ? document.body.innerText.length : 0;
  const includes = (marker) => html.includes(marker);
  const vendorTitle = new RegExp(signals.vendorTitle, "i");
  const verification = new RegExp(signals.verification, "i");
  const verificationTexts = Array.from(
    document.querySelectorAll(signals.verificationTags.join(",")),
    (element) => element.textContent || ""
  );
  const small = textLength < signals.interstitialMaxText;
  const vendor = (vendorTitle.test(document.title) && small) || signals.markers.some(includes);
  const hinted = verificationTexts.some((text) => verification.test(text))
    || signals.sdkMarkers.some((group) => group.every(includes));
  let found = true;
  if (config.selector !== null) {
    try { found = document.querySelector(config.selector) !== null; } catch (e) { found = false; }
  }
  const state = config.observe ? (globalThis[config.stateKey] || install()) : null;
  const nodeCount = document.getElementsByTagName("*").length;
  return {
    ready: document.readyState,
    growth: state ? state.growth : textLength + ":" + nodeCount,
    challenge: vendor || (hinted && small),
    found: found,
    placeholders: placeholderShown(state),
  };
})()"""


class ReadinessEnd(Enum):
    """Why the readiness wait stopped; also sent as ``X-Render-Ready-Reason``."""

    SETTLED = "settled"
    """No content request and no DOM growth for the quiet window."""
    LOAD_BUDGET_EXPIRED = "load-budget-expired"
    """The DOM stopped growing only after the load budget, with network activity ignored."""
    WAIT_FOR_FOUND = "wait-for-found"
    """The ``wait_for`` element was present long enough; the DOM never stopped growing."""
    DEADLINE = "deadline"
    """The budget ran out while the content was still changing."""
    CHALLENGE = "challenge"
    """A challenge was still shown at the deadline."""
    CHALLENGE_PERSISTED = "challenge-persisted"
    """A challenge outlasted the render's challenge patience, before the deadline."""
    ELEMENT_MISSING = "element-missing"
    """The ``wait_for`` element had not appeared at the deadline."""
    INTERRUPTED = "interrupted"
    """The wait was cancelled before it decided, e.g. by the worker's hard deadline."""


class NetworkActivity(Protocol):
    """Source of the in-flight request count (``network_activity.InflightTracker``)."""

    def snapshot(self) -> NetworkSnapshot: ...


class _NoNetwork:
    """Used when no tracker is attached: the network never holds the page back."""

    def snapshot(self) -> NetworkSnapshot:
        return IDLE_NETWORK


@dataclass(frozen=True)
class ReadinessOptions:
    """The activity sources of one render.

    ``activity`` must be the log the ``network`` tracker records into, so that the
    quiet window sees network and DOM events on one timeline and one clock.
    """

    activity: ActivityLog = field(default_factory=ActivityLog)
    network: NetworkActivity = field(default_factory=_NoNetwork)


@dataclass(frozen=True)
class ReadinessHints:
    """Floors learned from earlier renders of the same site section (``profiles``).

    Both can only lengthen the wait: the page cannot be declared ready before
    ``min_ready_seconds`` after the wait began, and the quiet window is at least
    ``quiet_floor_seconds``. Only observations of the current render end a wait.
    The defaults change nothing.
    """

    min_ready_seconds: float = 0.0
    quiet_floor_seconds: float = 0.0


COLD = ReadinessHints()


@dataclass(frozen=True)
class ContentTiming:
    """When content grew during one render, relative to the start of its readiness wait.

    ``last_growth_seconds`` is the time of the last DOM growth (0 if the first
    observation already showed the final content); ``largest_gap_seconds`` the
    longest idle gap between two content events.
    """

    last_growth_seconds: float
    largest_gap_seconds: float


@dataclass(frozen=True)
class WaitTarget:
    """What to wait for, for how long, and the learned floors of the section.

    ``challenge_patience_seconds``, if set, gives up on a challenge that is still shown
    that long after it was first seen, instead of waiting for the deadline: the caller
    has a better route to try. ``None`` waits for the deadline.
    """

    wait_for: str | None
    budget_seconds: float
    hints: ReadinessHints = COLD
    challenge_patience_seconds: float | None = None


@dataclass(frozen=True)
class PageState:
    """One observation of the page.

    ``growth`` changes whenever new content appeared since the previous
    observation; ``None`` means the observation failed (document being replaced).
    """

    ready_state: str
    challenge: bool
    found: bool
    placeholders: bool
    growth: tuple[Any, ...] | None

    @property
    def observed(self) -> bool:
        return self.growth is not None


NOT_READY = PageState("", False, False, False, None)


CHALLENGE_SIGNALS = {
    "vendorTitle": CHALLENGE_TITLE_PATTERN.pattern,
    "markers": list(CHALLENGE_BODY_MARKERS),
    "verification": HUMAN_VERIFICATION_PATTERN.pattern,
    "verificationTags": list(VERIFICATION_TEXT_TAGS),
    "sdkMarkers": [list(group) for group in CHALLENGE_SDK_MARKERS],
    "interstitialMaxText": INTERSTITIAL_MAX_TEXT_CHARS,
}


def build_observer_script(wait_for: str | None, observe: bool) -> str:
    """Return the JavaScript observer; all values are JSON-encoded, never interpolated raw.

    :param observe: install the growth-tracking ``MutationObserver`` (isolated world
        only); without it, growth is reported as a text-length/node-count fingerprint.
    """
    config = {
        "selector": wait_for,
        "observe": observe,
        "stateKey": OBSERVER_STATE_KEY,
        "signals": CHALLENGE_SIGNALS,
        "maxSeenTexts": MAX_SEEN_TEXTS,
        "nonContentTags": list(NON_CONTENT_TAGS),
        "loadingText": LOADING_TEXT_PATTERN.pattern,
        "loadingTextMaxChars": LOADING_TEXT_MAX_CHARS,
        "placeholderName": LOADING_PLACEHOLDER_NAME_PATTERN.pattern,
        "busySelector": LOADING_PLACEHOLDER_ATTRIBUTE_SELECTOR,
        "placeholderSelector": placeholder_candidate_selector(),
        "maxPlaceholderCandidates": MAX_PLACEHOLDER_CANDIDATES,
    }
    return OBSERVER_SCRIPT_TEMPLATE % {"config": json.dumps(config)}


def _page_state(raw: dict, growth: tuple[Any, ...]) -> PageState:
    return PageState(
        ready_state=str(raw.get("ready", "")),
        challenge=bool(raw.get("challenge", False)),
        found=bool(raw.get("found", False)),
        placeholders=bool(raw.get("placeholders", False)),
        growth=growth,
    )


class PageObserver:
    """Evaluates the observer script, in an isolated world when possible.

    The isolated world dies with its document; after a navigation (redirect,
    solved challenge) the next observation creates a new one, which counts as
    growth because its token differs. If the script itself fails in the isolated
    world, the observer stays in the main world for the rest of the render instead
    of installing a new observer on every poll. ``in_main_world`` tells whether the
    last observation had to fall back to the main world.

    :raises NavigationError: from ``observe`` once ``MAX_TARGET_GONE_FAILURES``
        observations in a row failed because the tab crashed or was detached.
    """

    def __init__(self, tab: CdpTab, wait_for: str | None):
        self._tab = tab
        self._observing_script = build_observer_script(wait_for, observe=True)
        self._fallback_script = build_observer_script(wait_for, observe=False)
        self._context: cdp.runtime.ExecutionContextId | None = None
        self._generation = 0
        self._isolated_failed = False
        self._warned = False
        self._target_gone_failures = 0
        self.in_main_world = False

    async def observe(self) -> PageState:
        """One observation; ``NOT_READY`` while the document is being replaced."""
        if self._context is None and not self._isolated_failed:
            await self._create_world()
        if self._context is None:
            return await self._observe_main_world()
        try:
            remote, exception = await self._tab.send(
                evaluation(self._observing_script, self._context)
            )
        except ProtocolException as exc:
            # The world's document was replaced; a new world is created next time.
            self._context = None
            return self._failed(exc)
        if exception is not None:
            self._abandon_isolated_world(exception)
            return await self._observe_main_world()
        return self._observed(remote.value or {}, self._generation, main_world=False)

    async def _create_world(self) -> None:
        try:
            self._context = await create_isolated_world(self._tab, ISOLATED_WORLD_NAME)
        except ProtocolException as exc:
            self._warn_once(str(exc))
            return
        self._generation += 1

    def _abandon_isolated_world(self, exception: cdp.runtime.ExceptionDetails) -> None:
        self._context = None
        self._isolated_failed = True
        self._warn_once(exception.text)

    async def _observe_main_world(self) -> PageState:
        try:
            raw = await evaluate(self._tab, self._fallback_script) or {}
        except ProtocolException as exc:
            return self._failed(exc)
        return self._observed(raw, "main-world", main_world=True)

    def _observed(self, raw: dict, world: Any, main_world: bool) -> PageState:
        self._target_gone_failures = 0
        self.in_main_world = main_world
        return _page_state(raw, (world, raw.get("growth")))

    def _failed(self, exc: ProtocolException) -> PageState:
        # A replaced document fails one or two observations and recovers; a crashed or
        # detached tab fails every one, and waiting for the deadline would only hide that.
        reason = str(exc).lower()
        gone = any(marker in reason for marker in TARGET_GONE_MARKERS)
        self._target_gone_failures = self._target_gone_failures + 1 if gone else 0
        if self._target_gone_failures >= MAX_TARGET_GONE_FAILURES:
            raise NavigationError("The browser tab crashed or was closed while rendering")
        return NOT_READY

    def _warn_once(self, reason: str) -> None:
        if self._warned:
            return
        self._warned = True
        log.warning(
            "Isolated world unavailable (%s); readiness compares text length and node "
            "count in the page's main world instead",
            reason,
        )


class _Progress:
    """What the polls have seen so far, on the clock of the activity log."""

    def __init__(self, activity: ActivityLog):
        self.activity = activity
        self.started = activity.now()
        self.token: tuple[Any, ...] | None = None
        # The deadline decides on what the page last showed, not on a poll that
        # failed because the document was being replaced at that moment.
        self.last_observed: PageState | None = None
        self.last_growth = self.started
        self.found_since: float | None = None
        self.interactive_since: float | None = None
        # Start of the current uninterrupted run of polls that saw a challenge.
        self.challenge_since: float | None = None
        # Two consecutive identical observations are the least evidence that the page is
        # not changing; a single one says nothing, whatever the quiet window.
        self.confirmed = False

    def update(self, poll: "_Poll") -> None:
        state, now = poll.state, poll.now
        self._track_growth(poll)
        self._track_loading(state, now)
        loaded = self.loaded(state, now, poll.load_budget_spent)
        usable = state.found and loaded and not state.challenge
        if not usable:
            self.found_since = None
        elif self.found_since is None:
            self.found_since = now

    def _track_growth(self, poll: "_Poll") -> None:
        state = poll.state
        self.confirmed = state.observed and state.growth == self.token
        if not state.observed:
            return
        self.last_observed = state
        self._track_challenge(state, poll.now)
        # The first observation is the baseline, not growth: recording it would put
        # the time navigation took into the largest gap and inflate the quiet window.
        if self.token is not None and state.growth != self.token:
            idle = poll.network.inflight == 0
            self.last_growth = self.activity.record(idle_before=idle)
        self.token = state.growth

    def _track_challenge(self, state: PageState, now: float) -> None:
        # Only an observation without a challenge ends the run: a failed poll during the
        # document swap of a solved challenge says nothing about the next document.
        if not state.challenge:
            self.challenge_since = None
        elif self.challenge_since is None:
            self.challenge_since = now

    def _track_loading(self, state: PageState, now: float) -> None:
        if state.ready_state not in (READY_STATE_INTERACTIVE, READY_STATE_COMPLETE):
            self.interactive_since = None
        elif self.interactive_since is None:
            self.interactive_since = now

    def loaded(self, state: PageState, now: float, load_budget_spent: bool) -> bool:
        if state.ready_state == READY_STATE_COMPLETE or load_budget_spent:
            return True
        since = self.interactive_since
        return since is not None and now - since >= LOAD_WATCHDOG_SECONDS

    def last_event(self) -> float:
        """Latest content event (network or DOM growth), or the start of the wait."""
        return max(self.last_growth, self.activity.last or self.started)


@dataclass(frozen=True)
class _Poll:
    """Everything one poll decides on."""

    state: PageState
    now: float
    load_budget_spent: bool
    network: NetworkSnapshot


def _quiet_for(elapsed: float, window: float) -> bool:
    # Strictly positive: growth seen in this very poll is never quiet, even with a
    # zero window.
    return elapsed > 0 and elapsed >= window


class ReadinessWaiter:
    """Polls a tab until it is ready or the time budget is spent.

    ``end`` is the ``ReadinessEnd`` once ``wait`` has returned or raised (``None``
    before, or if the wait was cancelled); ``challenge_polls`` counts the polls
    that saw a challenge, including one that resolved later; ``quiet_seconds`` is
    the quiet window of the last poll; ``ignored_requests`` the requests the
    network tracker did not count.
    """

    def __init__(self, tab: CdpTab, target: WaitTarget, options: ReadinessOptions | None = None):
        self._observer = PageObserver(tab, target.wait_for)
        self._target = target
        self._options = options or ReadinessOptions()
        self.end: ReadinessEnd | None = None
        self.challenge_polls = 0
        self.quiet_seconds = BASE_QUIET_SECONDS
        self.ignored_requests = 0
        self._progress: _Progress | None = None

    async def wait(self) -> bool:
        """Wait for readiness.

        :return: ``True`` if the page became stable, ``False`` if it is returned
            while the content was still changing (``wait_for`` present but the
            DOM never settled, or the budget ran out).
        :raises TargetBlockedError: if a challenge is still shown at the deadline.
        :raises ChallengePersistedError: if a challenge outlasted
            ``challenge_patience_seconds``.
        :raises RenderTimeoutError: if ``wait_for`` had not appeared at the deadline.
        :raises NavigationError: if no observation succeeded before the deadline, or
            the tab crashed or was closed.
        """
        progress = _Progress(self._options.activity)
        self._progress = progress
        budget = self._target.budget_seconds
        load_deadline = progress.started + budget * LOAD_BUDGET_SHARE
        deadline = progress.started + budget
        while True:
            state = await self._observer.observe()
            now = progress.activity.now()
            network = self._options.network.snapshot()
            poll = _Poll(state, now, now >= load_deadline, network)
            outcome = self._decide(poll, progress)
            if outcome is not None:
                return outcome
            self._check_challenge_patience(progress, now)
            if now >= deadline:
                return self._at_deadline(progress.last_observed)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _decide(self, poll: _Poll, progress: _Progress) -> bool | None:
        state = poll.state
        self.challenge_polls += int(state.challenge)
        progress.update(poll)
        self.ignored_requests = poll.network.ignored
        self.quiet_seconds = self._quiet_window(state)
        if self._is_ready(poll, progress):
            spent = poll.load_budget_spent
            ended = ReadinessEnd.LOAD_BUDGET_EXPIRED if spent else ReadinessEnd.SETTLED
            return self._finish(ended, stable=True)
        return self._shortcut(poll, progress)

    def _quiet_window(self, state: PageState) -> float:
        rhythm = QUIET_GAP_FACTOR * self._options.activity.max_gap
        floor = self._target.hints.quiet_floor_seconds
        window = min(MAX_QUIET_SECONDS, max(BASE_QUIET_SECONDS, rhythm, floor))
        if self._target.wait_for and state.found:
            return min(window, self._found_cap())
        return window

    def _found_cap(self) -> float:
        # Learned floors only lengthen: a section known to need a longer quiet window
        # keeps it even when the wait_for element is already there.
        return max(WAIT_FOR_QUIET_CAP_SECONDS, self._target.hints.quiet_floor_seconds)

    def _past_min_ready(self, progress: _Progress, now: float) -> bool:
        # Capped at the budget, so a learned floor beyond it cannot turn a settled
        # page into a deadline result.
        floor = min(self._target.hints.min_ready_seconds, self._target.budget_seconds)
        return now - progress.started >= floor

    def _is_ready(self, poll: _Poll, progress: _Progress) -> bool:
        state = poll.state
        loaded = progress.loaded(state, poll.now, poll.load_budget_spent)
        if not loaded or not progress.confirmed or not state.found:
            return False
        if not self._past_min_ready(progress, poll.now):
            return False
        if state.challenge or state.placeholders:
            return False
        if poll.load_budget_spent:
            return _quiet_for(poll.now - progress.last_growth, self.quiet_seconds)
        quiet = _quiet_for(poll.now - progress.last_event(), self.quiet_seconds)
        return quiet and poll.network.inflight == 0

    def _shortcut(self, poll: _Poll, progress: _Progress) -> bool | None:
        if self._target.wait_for and self._found_long_enough(progress, poll.now):
            return self._finish(ReadinessEnd.WAIT_FOR_FOUND, stable=False)
        return None

    def _found_long_enough(self, progress: _Progress, now: float) -> bool:
        # Only with wait_for: the caller named an element that proves the
        # content is there. Without it, a still-changing page may still be
        # rendering and must not be cut short.
        if progress.found_since is None or not self._past_min_ready(progress, now):
            return False
        return now - progress.found_since >= self._found_cap()

    def content_timing(self) -> ContentTiming:
        """When content grew during the finished wait; zero before ``wait`` was called."""
        progress = self._progress
        if progress is None:
            return ContentTiming(0.0, 0.0)
        last_growth = progress.last_growth - progress.started
        return ContentTiming(last_growth, self._options.activity.max_gap)

    def late_watch(self) -> LateContentWatch | None:
        """A watch that continues observing the page where this wait stopped.

        ``None`` when the wait ended in the main-world fallback: its growth token
        (text length and node count) changes with every carousel rotation, so a
        watch could not tell late content from churn and must not teach anything.

        :raises RuntimeError: if ``wait`` was never called.
        """
        progress = self._progress
        if progress is None:
            raise RuntimeError("late_watch() needs a finished wait()")
        if self._observer.in_main_world:
            return None
        baseline = GrowthBaseline(progress.started, progress.token)
        return LateContentWatch(self._observer, baseline, progress.activity.now)

    def _check_challenge_patience(self, progress: _Progress, now: float) -> None:
        patience = self._target.challenge_patience_seconds
        since = progress.challenge_since
        if patience is None or since is None or now - since < patience:
            return
        self.end = ReadinessEnd.CHALLENGE_PERSISTED
        raise ChallengePersistedError(
            f"The target kept showing an anti-bot challenge for {patience:.0f}s"
        )

    def _finish(self, end: ReadinessEnd, stable: bool) -> bool:
        self.end = end
        return stable

    def _at_deadline(self, state: PageState | None) -> bool:
        if state is None:
            raise NavigationError("The page could not be read before the deadline")
        if state.challenge:
            self.end = ReadinessEnd.CHALLENGE
            raise TargetBlockedError("The target kept showing an anti-bot challenge")
        # The observer reports found=true without wait_for; the guard keeps a missing
        # element from ever being reported for a request that named none.
        if self._target.wait_for and not state.found:
            self.end = ReadinessEnd.ELEMENT_MISSING
            raise RenderTimeoutError(
                f"Timed out after {self._target.budget_seconds:.0f}s "
                f"waiting for '{self._target.wait_for}'"
            )
        return self._finish(ReadinessEnd.DEADLINE, stable=False)
