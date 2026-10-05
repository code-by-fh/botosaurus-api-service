"""Timeline of content-relevant events during one render.

Network starts and finishes (``network_activity``) and DOM growth (``readiness``)
are recorded into the same log, because the gap that matters for the adaptive
quiet window is the one between *any* two such events: a fetch that finishes
and the DOM growth its response causes a second later belong to one burst.

Only idle gaps count, i.e. gaps during which no content request was open. A slow
API call is waiting, not rhythm; counting it would stretch the quiet window
after every slow response.
"""

import time

from app.timing import Clock


class ActivityLog:
    """Last event time and the largest gap between two consecutive events.

    One log belongs to one render and is only touched from the event loop.
    """

    def __init__(self, clock: Clock = time.monotonic):
        self._clock = clock
        self.last: float | None = None
        self.max_gap = 0.0

    def now(self) -> float:
        """The current time of this log's clock; every reader of the log uses it."""
        return self._clock()

    def record(self, idle_before: bool = True) -> float:
        """Record an event now and return its time.

        :param idle_before: whether no content request was open since the previous
            event; only then does the gap count towards ``max_gap``.
        """
        now = self._clock()
        if idle_before and self.last is not None:
            self.max_gap = max(self.max_gap, now - self.last)
        self.last = now
        return now
