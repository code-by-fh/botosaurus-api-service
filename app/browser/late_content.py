"""Watches a returned page for content that appears after the readiness wait ended.

A page that inserts its content from a timer after a long silence looks
finished to the readiness check. The only way to learn that it was not is to
keep looking: after the response has gone out, the same tab is observed for a
few more seconds with the observer of the readiness wait, so its growth counter
and isolated world carry on where the wait stopped. The result feeds the
section profile (``scraping.profiles``) and can only lengthen later waits.

A watch that never managed to read the page saw nothing, which is not the same
as seeing no growth: since a clean watch admits a render as reference of an HTTP
verification, such a watch fails with ``WatchInconclusiveError`` instead.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from app.timing import Clock

# Module-level value read at call time, so tests can shorten it. Slower than the
# readiness poll: nothing waits for this result, and it runs on a held worker.
LATE_POLL_INTERVAL_SECONDS = 0.25


class GrowthState(Protocol):
    """The part of an observation the watch needs (``readiness.PageState``)."""

    @property
    def observed(self) -> bool: ...

    @property
    def growth(self) -> Any: ...


class GrowthObserver(Protocol):
    """Source of observations (``readiness.PageObserver``)."""

    async def observe(self) -> GrowthState: ...


class WatchInconclusiveError(Exception):
    """The watch did not read the page once during its window, so it proves nothing."""


@dataclass(frozen=True)
class GrowthBaseline:
    """Where the readiness wait stopped: its start time and its last growth token."""

    started: float
    token: Any


class LateContentWatch:
    """Polls one page after its return and reports when its content last grew."""

    def __init__(self, observer: GrowthObserver, baseline: GrowthBaseline, clock: Clock):
        """:param clock: the clock of the render's activity log, so times are comparable."""
        self._observer = observer
        self._baseline = baseline
        self._clock = clock

    async def watch(self, seconds: float, stop: asyncio.Event | None = None) -> float | None:
        """Observe for ``seconds``, or until ``stop`` is set.

        :param stop: ends the watch early, at the latest one poll interval plus one
            observation after it is set.
        :return: when content last grew during the watch, in seconds after the start
            of the readiness wait; ``None`` if it did not grow.
        :raises WatchInconclusiveError: if no observation succeeded and the watch
            was not stopped.
        :raises ProtocolException: and other CDP errors if the browser fails; the
            caller decides how to log them.
        """
        token = self._baseline.token
        last_growth: float | None = None
        reads = 0
        until = self._clock() + seconds
        while self._clock() < until and not (stop and stop.is_set()):
            await asyncio.sleep(LATE_POLL_INTERVAL_SECONDS)
            state = await self._observer.observe()
            if not state.observed:
                continue
            reads += 1
            if state.growth != token:
                token = state.growth
                last_growth = self._clock()
        stopped = stop is not None and stop.is_set()
        return self._result(reads, last_growth, stopped)

    def _result(self, reads: int, last_growth: float | None, stopped: bool) -> float | None:
        # A stopped watch reports nothing anyway (its caller discards the result).
        if reads == 0 and not stopped:
            raise WatchInconclusiveError("The page could not be read during the watch")
        if last_growth is None:
            return None
        return last_growth - self._baseline.started
