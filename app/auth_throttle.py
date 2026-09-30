"""Locks out clients that keep presenting wrong API keys.

A sliding window per client address counts failed attempts. The state lives in
memory of this process and is bounded, so a flood of spoofed or rotating
addresses cannot exhaust memory; the oldest tracked client is evicted first.
"""

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

MAX_AUTH_FAILURES = 10
AUTH_FAILURE_WINDOW_SECONDS = 5 * 60
MAX_TRACKED_CLIENTS = 10_000
MIN_RETRY_AFTER_SECONDS = 1

Clock = Callable[[], float]


@dataclass(frozen=True)
class ThrottlePolicy:
    """How many failures within which window lock a client out, and how many clients to track."""

    max_failures: int = MAX_AUTH_FAILURES
    window_seconds: float = AUTH_FAILURE_WINDOW_SECONDS
    max_tracked_clients: int = MAX_TRACKED_CLIENTS


class FailedAuthLimiter:
    """Sliding-window counter of failed authentication attempts per client.

    Used from the event loop only, so it needs no lock.
    """

    def __init__(self, policy: ThrottlePolicy | None = None, clock: Clock = time.monotonic):
        self._policy = policy or ThrottlePolicy()
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def retry_after_seconds(self, client: str) -> int:
        """Return how long ``client`` stays locked out; ``0`` when it may try again."""
        failures = self._recent_failures(client)
        if len(failures) < self._policy.max_failures:
            return 0
        unlocks_at = failures[0] + self._policy.window_seconds
        return max(MIN_RETRY_AFTER_SECONDS, math.ceil(unlocks_at - self._clock()))

    def record_failure(self, client: str) -> None:
        """Count one failed attempt of ``client``."""
        failures = self._recent_failures(client)
        failures.append(self._clock())
        self._failures[client] = failures
        self._failures.move_to_end(client)
        while len(self._failures) > self._policy.max_tracked_clients:
            self._failures.popitem(last=False)

    def _recent_failures(self, client: str) -> deque[float]:
        # Only the newest max_failures timestamps matter: the oldest of them
        # decides when the client drops below the limit again.
        failures = self._failures.get(client) or deque(maxlen=self._policy.max_failures)
        horizon = self._clock() - self._policy.window_seconds
        while failures and failures[0] <= horizon:
            failures.popleft()
        if not failures:
            self._failures.pop(client, None)
        return failures
