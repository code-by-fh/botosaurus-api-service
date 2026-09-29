"""Caps simultaneous requests per target host.

Many parallel requests from one IP to one site are a strong bot signal and a
fast way to get rate-limited, no matter how good the browser fingerprint is.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.errors import ServiceBusyError


class HostLimiter:
    """Per-host semaphores that are dropped again once no request uses them."""

    def __init__(self, max_per_host: int, wait_timeout_seconds: float):
        self._max_per_host = max_per_host
        self._wait_timeout = wait_timeout_seconds
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._users: dict[str, int] = {}

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        """Hold one of the host's slots for the duration of the ``async with`` block.

        :raises ServiceBusyError: if no slot for ``host`` became free in time.
        """
        semaphore = self._enter(host)
        try:
            try:
                async with asyncio.timeout(self._wait_timeout):
                    await semaphore.acquire()
            except TimeoutError as exc:
                raise ServiceBusyError(f"Too many concurrent requests for {host}") from exc
            try:
                yield
            finally:
                semaphore.release()
        finally:
            self._leave(host)

    def _enter(self, host: str) -> asyncio.Semaphore:
        if host not in self._semaphores:
            self._semaphores[host] = asyncio.Semaphore(self._max_per_host)
        self._users[host] = self._users.get(host, 0) + 1
        return self._semaphores[host]

    def _leave(self, host: str) -> None:
        self._users[host] -= 1
        if self._users[host] == 0:
            del self._users[host]
            del self._semaphores[host]
