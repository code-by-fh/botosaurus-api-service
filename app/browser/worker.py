"""A pool slot that owns one browser and replaces it before it degrades.

Long-lived Chrome processes get measurably slower and larger over time, so a
browser is restarted after ``max_pages`` renders, after ``max_age_seconds``,
or as soon as a render left it in an unknown state.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from app.browser.page_loader import BrowserJob, BrowserPage, load_page
from app.browser.session import BrowserSession, SessionFactory
from app.errors import RenderTimeoutError, ServiceError

log = logging.getLogger("botosaurus.browser")

HARD_DEADLINE_GRACE_SECONDS = 10.0

Clock = Callable[[], float]


class BrowserWorker:
    """Owns at most one ``BrowserSession`` at a time; not safe for concurrent renders."""

    def __init__(self, factory: SessionFactory, clock: Clock = time.monotonic):
        self._factory = factory
        self._settings = factory.settings
        self._clock = clock
        self._session: BrowserSession | None = None
        self._pages = 0
        self._started_at = 0.0

    async def start(self) -> None:
        """Launch the browser (no-op if one is already running)."""
        if self._session is not None:
            return
        self._session = await self._factory.start()
        self._pages = 0
        self._started_at = self._clock()

    async def stop(self) -> None:
        """Stop the browser if one is running."""
        session, self._session = self._session, None
        if session is not None:
            await session.stop()

    async def restart(self) -> None:
        """Replace the browser with a fresh one."""
        await self.stop()
        await self.start()

    @property
    def needs_restart(self) -> bool:
        if self._session is None or not self._session.healthy:
            return True
        too_old = self._clock() - self._started_at >= self._settings.max_age_seconds
        return too_old or self._pages >= self._settings.max_pages

    async def render(self, job: BrowserJob) -> BrowserPage:
        """Render ``job``; the browser is flagged for restart on any unexpected failure.

        A hard deadline above the job's own timeout catches CDP calls that hang
        forever, which would otherwise block this slot permanently.

        :raises ServiceError: subclasses for navigation, timeout and blocking failures.
        """
        if self.needs_restart:
            await self.restart()
        session = self._session
        self._pages += 1
        try:
            async with asyncio.timeout(job.timeout_seconds + HARD_DEADLINE_GRACE_SECONDS):
                return await load_page(session, job)
        except ServiceError:
            raise
        except TimeoutError as exc:
            session.mark_unhealthy()
            raise RenderTimeoutError(
                f"Browser did not respond within {job.timeout_seconds}s"
            ) from exc
        except Exception:
            session.mark_unhealthy()
            raise
