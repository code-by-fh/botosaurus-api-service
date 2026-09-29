"""Loads one URL in an isolated browser context and returns the rendered DOM."""

import asyncio
import logging
from dataclasses import dataclass

from zendriver import Tab, cdp
from zendriver.core.connection import ProtocolException

from app.browser.readiness import ReadinessTimings, ReadinessWaiter, WaitTarget
from app.browser.session import BrowserSession
from app.errors import NavigationError

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


@dataclass(frozen=True)
class BrowserJob:
    """What to render and how."""

    url: str
    wait_for: str | None
    timeout_seconds: float
    use_proxy: bool
    block_resources: bool
    idle_timeout_seconds: float | None = None


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
        await _block_resources(tab)
    await _navigate(tab, job.url)
    remaining = job.timeout_seconds - (loop.time() - started)
    budget = max(remaining, MIN_READINESS_BUDGET_SECONDS)
    timings = ReadinessTimings(idle_give_up_seconds=job.idle_timeout_seconds)
    stable = await ReadinessWaiter(tab, WaitTarget(job.wait_for, budget), timings).wait()
    if not stable:
        log.warning("Content of %s was still changing at the deadline", job.url)
    return await _read_page(tab, job.url, stable)


async def _read_page(tab: Tab, url: str, stable: bool) -> BrowserPage:
    try:
        html = await tab.get_content()
        final_url = await tab.evaluate("location.href")
        status = await tab.evaluate(UPSTREAM_STATUS_SCRIPT)
    except ProtocolException as exc:
        raise NavigationError(f"{url} navigated away while its content was read") from exc
    return BrowserPage(html=html, final_url=str(final_url), stable=stable, status=int(status or 0))


async def load_page(session: BrowserSession, job: BrowserJob) -> BrowserPage:
    """Render ``job`` in a fresh context of ``session``; the context is always disposed.

    :raises NavigationError: if Chrome reports a navigation failure.
    :raises RenderTimeoutError: if ``job.wait_for`` never appeared.
    :raises TargetBlockedError: if an anti-bot challenge did not resolve.
    """
    tab = await session.open_tab(job.use_proxy)
    try:
        return await _render_in_tab(tab, job)
    finally:
        await _close_or_flag(session, tab)


async def _close_or_flag(session: BrowserSession, tab: Tab) -> None:
    # Any failure while disposing leaves the browser in an unknown state. It is
    # not re-raised (that would mask the render result or error); the session is
    # flagged instead so its worker restarts Chrome before the next request.
    try:
        await session.close_tab(tab)
    except Exception:
        log.warning("Could not dispose browser context, restarting browser", exc_info=True)
        session.mark_unhealthy()
