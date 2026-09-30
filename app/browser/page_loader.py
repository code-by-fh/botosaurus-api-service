"""Loads one URL in an isolated browser context and returns the rendered DOM."""

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from zendriver import Tab, cdp
from zendriver.core.connection import ProtocolException

from app.browser.clearance import ClearanceScope, ClearanceVisit
from app.browser.readiness import (
    DEFAULT_TIMINGS,
    ReadinessTimings,
    ReadinessWaiter,
    WaitTarget,
)
from app.browser.session import BrowserSession, ContextCookies
from app.errors import NavigationError, TargetBlockedError
from app.scraping.clearance import ClearanceStore, route_for
from app.timing import Note, Phase, PhaseTimer

log = logging.getLogger("render.browser")

BLOCKED_RESOURCE_PATTERNS = (
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.avif",
    "*.svg",
    "*.ico",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.otf",
    "*.mp4",
    "*.webm",
    "*.mp3",
    "*.m4a",
    "*.css",
)
MIN_READINESS_BUDGET_SECONDS = 1.0
UPSTREAM_STATUS_SCRIPT = '(performance.getEntriesByType("navigation")[0] || {}).responseStatus || 0'
# Logged when the wait was cancelled before it decided, e.g. by the worker's hard deadline.
READINESS_INTERRUPTED = "interrupted"
# Only a page the site actually served proves the clearance cookies valid;
# an error page may come with a token the site no longer accepts.
HARVEST_STATUS_RANGE = range(200, 400)


@dataclass(frozen=True)
class BrowserJob:
    """What to render and how."""

    url: str
    wait_for: str | None
    timeout_seconds: float
    use_proxy: bool
    block_resources: bool
    idle_timeout_seconds: float | None = None
    wait_for_settle_seconds: float | None = None
    timer: PhaseTimer = field(default_factory=PhaseTimer, compare=False, repr=False)


@dataclass(frozen=True)
class BrowserPage:
    """Rendered document.

    ``stable`` is False if content was still changing at the deadline;
    ``status`` is the HTTP status of the main document (0 if unknown).
    """

    html: str
    final_url: str
    stable: bool
    status: int


async def _block_resources(tab: Tab) -> None:
    await tab.send(cdp.network.enable())
    await tab.send(cdp.network.set_blocked_ur_ls(urls=list(BLOCKED_RESOURCE_PATTERNS)))


async def _navigate(tab: Tab, url: str) -> None:
    _, _, error_text, _ = await tab.send(cdp.page.navigate(url))
    if error_text:
        raise NavigationError(f"Browser could not load {url}: {error_text}")


async def _render_in_tab(tab: Tab, job: BrowserJob) -> BrowserPage:
    loop = asyncio.get_running_loop()
    started = loop.time()
    if job.block_resources:
        with job.timer.phase(Phase.CONTEXT):
            await _block_resources(tab)
    with job.timer.phase(Phase.NAVIGATE):
        await _navigate(tab, job.url)
    remaining = job.timeout_seconds - (loop.time() - started)
    budget = max(remaining, MIN_READINESS_BUDGET_SECONDS)
    waiter = ReadinessWaiter(tab, WaitTarget(job.wait_for, budget), _readiness_timings(job))
    stable = await _await_readiness(waiter, job.timer)
    if not stable:
        log.warning("Content of %s was still changing at the deadline", job.url)
    with job.timer.phase(Phase.READ):
        return await _read_page(tab, job.url, stable)


def _readiness_timings(job: BrowserJob) -> ReadinessTimings:
    settle = job.wait_for_settle_seconds
    if settle is None:
        settle = DEFAULT_TIMINGS.found_settle_seconds
    return ReadinessTimings(
        found_settle_seconds=settle, idle_give_up_seconds=job.idle_timeout_seconds
    )


async def _await_readiness(waiter: ReadinessWaiter, timer: PhaseTimer) -> bool:
    try:
        with timer.phase(Phase.READINESS):
            return await waiter.wait()
    finally:
        end = waiter.end.value if waiter.end is not None else READINESS_INTERRUPTED
        timer.note(Note.READINESS_END, end)
        timer.note(Note.CHALLENGE_POLLS, waiter.challenge_polls)


async def _read_page(tab: Tab, url: str, stable: bool) -> BrowserPage:
    try:
        html = await tab.get_content()
        final_url = await tab.evaluate("location.href")
        status = await tab.evaluate(UPSTREAM_STATUS_SCRIPT)
    except ProtocolException as exc:
        raise NavigationError(f"{url} navigated away while its content was read") from exc
    return BrowserPage(html=html, final_url=str(final_url), stable=stable, status=int(status or 0))


async def load_page(
    session: BrowserSession, job: BrowserJob, clearance: ClearanceStore | None = None
) -> BrowserPage:
    """Render ``job`` in a fresh context of ``session``; the context is always disposed.

    :param clearance: store of anti-bot clearance cookies to reuse; ``None`` disables reuse.
    :raises NavigationError: if Chrome reports a navigation failure.
    :raises RenderTimeoutError: if ``job.wait_for`` never appeared.
    :raises TargetBlockedError: if an anti-bot challenge did not resolve.
    """
    with job.timer.phase(Phase.CONTEXT):
        tab = await session.open_tab(job.use_proxy)
    try:
        visit = _clearance_visit(clearance, session.context_cookies(tab), job)
        if visit is None:
            return await _render_in_tab(tab, job)
        return await _render_reusing_clearance(tab, job, visit)
    finally:
        await _close_or_flag(session, tab)


def _clearance_visit(
    store: ClearanceStore | None, cookies: ContextCookies | None, job: BrowserJob
) -> ClearanceVisit | None:
    if store is None or cookies is None:
        return None
    scope = ClearanceScope(route_for(job.use_proxy), (urlsplit(job.url).hostname or "").lower())
    return ClearanceVisit(store, cookies, scope)


async def _render_reusing_clearance(
    tab: Tab, job: BrowserJob, visit: ClearanceVisit
) -> BrowserPage:
    try:
        with job.timer.phase(Phase.CONTEXT):
            await visit.inject()
        page = await _render_in_tab(tab, job)
        if page.status in HARVEST_STATUS_RANGE:
            with job.timer.phase(Phase.CONTEXT):
                await visit.harvest()
        return page
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
