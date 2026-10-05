"""Loads one URL in an isolated browser context and returns the rendered DOM.

The context is disposed when the render is done, except when the job's
``learning`` hook asks to watch the returned page for late content: then the
tab is handed over in a ``LingeringRender``, which observes it and disposes it
afterwards (``pool`` decides whether and when that runs).
"""

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Protocol
from urllib.parse import urlsplit

from zendriver import Tab, cdp
from zendriver.core.connection import ProtocolException

from app.browser.activity import ActivityLog
from app.browser.blocking import (
    BlockedResource,
    block_patterns,
    default_tracker_domains,
    foreign_tracker_domains,
)
from app.browser.clearance import ClearanceScope, ClearanceVisit
from app.browser.evaluation import create_isolated_world, evaluate
from app.browser.late_content import LateContentWatch, WatchInconclusiveError
from app.browser.network_activity import InflightTracker
from app.browser.readiness import (
    COLD,
    ContentTiming,
    ReadinessEnd,
    ReadinessHints,
    ReadinessOptions,
    ReadinessWaiter,
    WaitTarget,
)
from app.browser.session import BrowserSession, ContextCookies
from app.config import DEFAULT_MAX_RESPONSE_BYTES
from app.errors import (
    NavigationError,
    RenderTimeoutError,
    ResponseTooLargeError,
    TargetBlockedError,
)
from app.log_safety import loggable_url
from app.scraping.clearance import ClearanceStore, route_for
from app.timing import MILLISECONDS_PER_SECOND, Clock, Note, Phase, PhaseTimer

log = logging.getLogger("render.browser")

MIN_READINESS_BUDGET_SECONDS = 1.0
UPSTREAM_STATUS_SCRIPT = '(performance.getEntriesByType("navigation")[0] || {}).responseStatus || 0'
HTML_LENGTH_SCRIPT = "document.documentElement ? document.documentElement.outerHTML.length : 0"
# The size check runs in its own isolated world: there the prototype getters are
# not the page's, so the page cannot fake the length of its document.
SIZE_CHECK_WORLD_NAME = "render-size-check"
# Only a page the site actually served proves the clearance cookies valid;
# an error page may come with a token the site no longer accepts.
HARVEST_STATUS_RANGE = range(200, 400)
# A late-content watch whose CDP calls hang must not hold its worker forever.
LATE_WATCH_GRACE_SECONDS = 5.0
NO_GROWTH = ContentTiming(0.0, 0.0)


@dataclass(frozen=True)
class BrowserPage:
    """Rendered document.

    ``stable`` is False if content was still changing at the deadline;
    ``status`` is the HTTP status of the main document (0 if unknown);
    ``ready_reason`` is the ``ReadinessEnd`` value that ended the wait;
    ``timing`` tells when its content grew during the wait.
    """

    html: str
    final_url: str
    stable: bool
    status: int
    ready_reason: str = ReadinessEnd.SETTLED.value
    timing: ContentTiming = NO_GROWTH


class RenderLearning(Protocol):
    """Per-request hook through which the scraping layer learns from a render."""

    @property
    def observe_seconds(self) -> float:
        """How long to watch the returned page for late content."""
        ...

    def rendered(self, page: BrowserPage) -> bool:
        """Called once the page was read; ``True`` asks to watch it for late content."""
        ...

    def observed(self, late_growth_seconds: float | None) -> None:
        """Called when a watch finished; never after a cancelled or failed watch."""
        ...


@dataclass(frozen=True)
class BrowserJob:
    """What to render and how; ``hints`` are the learned floors of the section.

    ``challenge_patience_seconds`` is handed to the readiness wait (``WaitTarget``): set,
    a challenge shown that long ends the render with ``ChallengePersistedError`` before
    the deadline.
    """

    url: str
    wait_for: str | None
    timeout_seconds: float
    use_proxy: bool
    block_resources: frozenset[BlockedResource]
    timer: PhaseTimer = field(default_factory=PhaseTimer, compare=False, repr=False)
    hints: ReadinessHints = COLD
    learning: RenderLearning | None = field(default=None, compare=False, repr=False)
    challenge_patience_seconds: float | None = None


@dataclass(frozen=True)
class LoadServices:
    """Shared collaborators of every render on one worker.

    ``max_html_chars`` is the largest document a render may return.
    """

    clearance: ClearanceStore | None = None
    clock: Clock = time.monotonic
    max_html_chars: int = DEFAULT_MAX_RESPONSE_BYTES


@dataclass(frozen=True)
class OpenTab:
    """A tab together with the session that has to dispose of it."""

    session: BrowserSession
    tab: Tab

    async def close(self) -> None:
        await _close_or_flag(self.session, self.tab)


class LingeringRender:
    """A returned render whose tab stays open to watch for late content.

    Exactly one of ``observe`` and ``discard`` must be awaited; both dispose of the tab.
    ``stop`` ends a running (or not yet started) observation early. It is used
    instead of task cancellation: a task cancelled before its first step never runs
    its ``finally`` blocks, which would leak the tab and the worker.
    """

    def __init__(self, tab: OpenTab, watch: LateContentWatch, learning: RenderLearning):
        self._tab = tab
        self._watch = watch
        self._learning = learning
        self._stop = asyncio.Event()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        """Cut the observation short; it then reports nothing."""
        self._stop.set()

    async def observe(self) -> None:
        """Watch, report to the learning hook and dispose of the tab.

        A failed watch is logged at WARNING and reports nothing: the response has
        long been sent, and a broken observation must not teach anything. Its
        browser is restarted, since the failure leaves it in an unknown state.
        A stopped observation (a queued request needs the worker) reports nothing
        either: it did not watch long enough to tell.
        """
        seconds = self._learning.observe_seconds
        try:
            async with asyncio.timeout(seconds + LATE_WATCH_GRACE_SECONDS):
                late = await self._watch.watch(seconds, self._stop)
        except WatchInconclusiveError:
            # The page kept failing to answer, not the browser: nothing is learned,
            # and Chrome needs no restart.
            log.info("Late-content observation could not read the page; nothing learned")
            return
        except Exception:
            log.warning("Late-content observation failed, restarting browser", exc_info=True)
            self._tab.session.mark_unhealthy()
            return
        finally:
            await self._tab.close()
        if not self.stopped:
            self._learning.observed(late)

    async def discard(self) -> None:
        """Dispose of the tab without watching."""
        await self._tab.close()


@dataclass(frozen=True)
class RenderOutcome:
    """The page, plus its still-open tab if the job asked to watch it for late content."""

    page: BrowserPage
    lingering: LingeringRender | None = None


@dataclass(frozen=True)
class _RenderedTab:
    page: BrowserPage
    waiter: ReadinessWaiter


async def _block_resources(tab: Tab, job: BrowserJob) -> None:
    patterns = block_patterns(job.block_resources)
    await tab.send(cdp.network.enable())
    await tab.send(cdp.network.set_blocked_ur_ls(url_patterns=patterns))


async def _navigate(tab: Tab, url: str, budget_seconds: float) -> None:
    """Navigate within ``budget_seconds``.

    ``Page.navigate`` only returns once the response is committed, so a server that
    delays its first byte would otherwise hold the render until the worker's hard
    deadline, which restarts a healthy Chrome. The tab is closed normally afterwards.

    :raises RenderTimeoutError: if the navigation did not commit within the budget.
    :raises NavigationError: if Chrome reports a navigation failure.
    """
    try:
        async with asyncio.timeout(budget_seconds):
            _, _, error_text, _ = await tab.send(cdp.page.navigate(url))
    except TimeoutError as exc:
        raise RenderTimeoutError(
            f"{url} did not start responding within {budget_seconds:.0f}s"
        ) from exc
    if error_text:
        raise NavigationError(f"Browser could not load {url}: {error_text}")


async def _render_in_tab(tab: Tab, job: BrowserJob, services: LoadServices) -> _RenderedTab:
    activity = ActivityLog(services.clock)
    host = urlsplit(job.url).hostname or ""
    tracker = InflightTracker(
        frozenset(foreign_tracker_domains(default_tracker_domains(), host)), activity
    )
    # Attached before navigation, so the document request and early scripts are seen.
    tracker.attach(tab)
    try:
        waiter, stable = await _load_and_wait(tab, job, ReadinessOptions(activity, tracker))
    finally:
        tracker.detach(tab)
    with job.timer.phase(Phase.READ):
        await _ensure_html_fits(tab, services.max_html_chars)
        page = await _read_page(tab, job.url, stable)
    # Second guard: the check above may not have been able to measure the document.
    _enforce_html_limit(len(page.html), services.max_html_chars)
    page = replace(page, ready_reason=_end_of(waiter), timing=waiter.content_timing())
    return _RenderedTab(page, waiter)


async def _load_and_wait(
    tab: Tab, job: BrowserJob, options: ReadinessOptions
) -> tuple[ReadinessWaiter, bool]:
    started = options.activity.now()
    if job.block_resources:
        with job.timer.phase(Phase.CONTEXT):
            await _block_resources(tab, job)
    with job.timer.phase(Phase.NAVIGATE):
        await _navigate(tab, job.url, job.timeout_seconds - (options.activity.now() - started))
    remaining = job.timeout_seconds - (options.activity.now() - started)
    budget = max(remaining, MIN_READINESS_BUDGET_SECONDS)
    target = WaitTarget(job.wait_for, budget, job.hints, job.challenge_patience_seconds)
    waiter = ReadinessWaiter(tab, target, options)
    stable = await _await_readiness(waiter, job.timer)
    if not stable:
        log.warning("Content of %s was still changing at the deadline", loggable_url(job.url))
    return waiter, stable


async def _ensure_html_fits(tab: Tab, max_chars: int) -> None:
    """Refuse a document too large to read, before Chrome serialises it into this process.

    A page can build hundreds of megabytes of DOM; reading that would exhaust the
    container's memory. Characters are counted, which never exceed the UTF-8 bytes.
    A length that cannot be measured counts as unknown: the page is read, and the
    caller enforces the limit on the returned document instead.

    :raises ResponseTooLargeError: if the document has more than ``max_chars`` characters.
    """
    length = await _measured_html_length(tab)
    if length is not None:
        _enforce_html_limit(length, max_chars)


async def _measured_html_length(tab: Tab) -> int | None:
    try:
        context = await _size_check_world(tab)
        length = await evaluate(tab, HTML_LENGTH_SCRIPT, context)
    except ProtocolException:
        log.info("Could not measure the rendered document; checking its size after reading")
        return None
    return length if isinstance(length, int) else None


async def _size_check_world(tab: Tab) -> cdp.runtime.ExecutionContextId | None:
    try:
        return await create_isolated_world(tab, SIZE_CHECK_WORLD_NAME)
    except ProtocolException:
        # Without an isolated world the main world still measures honest pages.
        return None


def _enforce_html_limit(length: int, max_chars: int) -> None:
    if length > max_chars:
        raise ResponseTooLargeError(f"The rendered page exceeds {max_chars} characters")


async def _await_readiness(waiter: ReadinessWaiter, timer: PhaseTimer) -> bool:
    try:
        with timer.phase(Phase.READINESS):
            return await waiter.wait()
    finally:
        timer.note(Note.READINESS_END, _end_of(waiter))
        timer.note(Note.CHALLENGE_POLLS, waiter.challenge_polls)
        timer.note(Note.QUIET_MS, round(waiter.quiet_seconds * MILLISECONDS_PER_SECOND))
        timer.note(Note.INFLIGHT_IGNORED, waiter.ignored_requests)


def _end_of(waiter: ReadinessWaiter) -> str:
    end = waiter.end if waiter.end is not None else ReadinessEnd.INTERRUPTED
    return end.value


async def _read_page(tab: Tab, url: str, stable: bool) -> BrowserPage:
    try:
        html = await tab.get_content()
        final_url = await evaluate(tab, "location.href")
        status = await evaluate(tab, UPSTREAM_STATUS_SCRIPT)
    except ProtocolException as exc:
        raise NavigationError(f"{url} navigated away while its content was read") from exc
    return BrowserPage(html=html, final_url=str(final_url), stable=stable, status=int(status or 0))


async def load_page(
    session: BrowserSession, job: BrowserJob, services: LoadServices | None = None
) -> RenderOutcome:
    """Render ``job`` in a fresh context of ``session``.

    The context is disposed before returning, unless the outcome carries a
    ``LingeringRender``, which then owns it.

    :param services: clearance store (``None`` disables reuse) and clock of the render.
    :raises NavigationError: if Chrome reports a navigation failure or the page
        could not be read.
    :raises RenderTimeoutError: if the navigation did not commit in time, or
        ``job.wait_for`` never appeared.
    :raises TargetBlockedError: if an anti-bot challenge did not resolve
        (``ChallengePersistedError`` if it outlasted ``job.challenge_patience_seconds``).
    """
    services = services or LoadServices()
    with job.timer.phase(Phase.CONTEXT):
        tab = OpenTab(session, await session.open_tab(job.use_proxy))
    lingering: LingeringRender | None = None
    try:
        rendered = await _render(tab, job, services)
        lingering = _lingering(tab, job, rendered)
        return RenderOutcome(rendered.page, lingering)
    finally:
        if lingering is None:
            await tab.close()


async def _render(tab: OpenTab, job: BrowserJob, services: LoadServices) -> _RenderedTab:
    render = functools.partial(_render_in_tab, tab.tab, job, services)
    cookies = tab.session.context_cookies(tab.tab)
    visit = _clearance_visit(services.clearance, cookies, job)
    if visit is None:
        return await render()
    return await _render_reusing_clearance(job, visit, render)


def _lingering(tab: OpenTab, job: BrowserJob, rendered: _RenderedTab) -> LingeringRender | None:
    learning = job.learning
    if learning is None or not learning.rendered(rendered.page):
        return None
    watch = rendered.waiter.late_watch()
    if watch is None:
        return None
    return LingeringRender(tab, watch, learning)


def _clearance_visit(
    store: ClearanceStore | None, cookies: ContextCookies | None, job: BrowserJob
) -> ClearanceVisit | None:
    if store is None or cookies is None:
        return None
    scope = ClearanceScope(route_for(job.use_proxy), (urlsplit(job.url).hostname or "").lower())
    return ClearanceVisit(store, cookies, scope)


async def _render_reusing_clearance(
    job: BrowserJob, visit: ClearanceVisit, render: Callable[[], Awaitable[_RenderedTab]]
) -> _RenderedTab:
    try:
        with job.timer.phase(Phase.CONTEXT):
            await visit.inject()
        rendered = await render()
        if rendered.page.status in HARVEST_STATUS_RANGE:
            with job.timer.phase(Phase.CONTEXT):
                await visit.harvest()
        return rendered
    except TargetBlockedError:
        visit.reject()
        raise
    finally:
        job.timer.note(Note.CLEARANCE, visit.outcome.value)


async def _close_or_flag(session: BrowserSession, tab: Tab) -> None:
    # Any failure while disposing leaves the browser in an unknown state. It is
    # not re-raised (that would mask the render result or error); the session is
    # flagged instead so its worker restarts Chrome before the next request.
    try:
        await session.close_tab(tab)
    except Exception:
        log.warning("Could not dispose browser context, restarting browser", exc_info=True)
        session.mark_unhealthy()
