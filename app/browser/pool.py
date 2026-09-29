"""Fixed-size pool of browser workers with a bounded wait queue.

Requests wait up to ``queue.timeout_seconds`` for a free worker instead of
failing immediately; beyond ``queue.max_waiting`` waiting requests the pool
answers ``SERVICE_BUSY`` at once so memory stays bounded on a small VPS.
Workers that need a restart are recycled in the background and rejoin the
pool afterwards.
"""

import asyncio
import logging
from dataclasses import dataclass

from app.browser.page_loader import BrowserJob, BrowserPage
from app.browser.worker import BrowserWorker
from app.config import QueueSettings
from app.errors import ServiceBusyError

log = logging.getLogger("render.pool")

RECYCLE_RETRY_SECONDS = 5.0


@dataclass(frozen=True)
class PoolStats:
    """Snapshot of pool utilisation for the health endpoint."""

    total: int
    idle: int
    busy: int
    recycling: int
    waiting: int
    restarts: int


class BrowserPool:
    """Hands out workers one request at a time and recycles them when due."""

    def __init__(self, workers: list[BrowserWorker], queue: QueueSettings):
        self._workers = workers
        self._queue_settings = queue
        self._idle: asyncio.Queue[BrowserWorker] = asyncio.Queue()
        self._recycling: set[asyncio.Task[None]] = set()
        self._waiting = 0
        self._restarts = 0

    async def start(self) -> None:
        """Launch all browsers one after another to avoid a CPU/RAM spike at boot."""
        for index, worker in enumerate(self._workers, start=1):
            await worker.start()
            log.info("Browser worker %d/%d ready", index, len(self._workers))
            self._idle.put_nowait(worker)

    async def render(self, job: BrowserJob) -> BrowserPage:
        """Render ``job`` on the next free worker.

        :raises ServiceBusyError: if the wait queue is full or no worker became free in time.
        """
        worker = await self._acquire()
        try:
            return await worker.render(job)
        finally:
            self._release(worker)

    async def _acquire(self) -> BrowserWorker:
        if self._idle.empty() and self._waiting >= self._queue_settings.max_waiting:
            raise ServiceBusyError("All browsers are busy and the wait queue is full")
        self._waiting += 1
        try:
            async with asyncio.timeout(self._queue_settings.timeout_seconds):
                return await self._idle.get()
        except TimeoutError as exc:
            raise ServiceBusyError("No browser became available in time") from exc
        finally:
            self._waiting -= 1

    def _release(self, worker: BrowserWorker) -> None:
        if not worker.needs_restart:
            self._idle.put_nowait(worker)
            return
        task = asyncio.create_task(self._recycle(worker))
        self._recycling.add(task)
        task.add_done_callback(self._recycling.discard)

    async def _recycle(self, worker: BrowserWorker) -> None:
        # Retries until Chrome starts again: a slot that silently disappeared
        # would shrink capacity forever without anyone noticing.
        while True:
            try:
                await worker.restart()
                break
            except Exception:
                log.error(
                    "Browser restart failed, retrying in %.0fs",
                    RECYCLE_RETRY_SECONDS,
                    exc_info=True,
                )
                await asyncio.sleep(RECYCLE_RETRY_SECONDS)
        self._restarts += 1
        self._idle.put_nowait(worker)

    def stats(self) -> PoolStats:
        """Return the current utilisation."""
        idle = self._idle.qsize()
        recycling = len(self._recycling)
        return PoolStats(
            total=len(self._workers),
            idle=idle,
            busy=len(self._workers) - idle - recycling,
            recycling=recycling,
            waiting=self._waiting,
            restarts=self._restarts,
        )

    async def shutdown(self) -> None:
        """Cancel pending restarts and stop every browser."""
        for task in list(self._recycling):
            task.cancel()
        await asyncio.gather(*self._recycling, return_exceptions=True)
        for worker in self._workers:
            await worker.stop()
