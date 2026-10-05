"""Hosts whose direct route was blocked by bot protection while HOME_PROXY got through.

The scraper sends later requests to a remembered host through HOME_PROXY from the
start, instead of paying for a blocked direct attempt every time. A successful
direct render (for a request that chose its route itself) does not clear an entry;
only its lifetime does, so a host is re-tried directly once it expired.

The store is bounded: beyond ``capacity`` the host remembered longest ago is
dropped, which can only send a request over the direct route again. All methods
are synchronous and never await, so no lock is needed on the event loop.
"""

from collections import OrderedDict
from collections.abc import Callable

Clock = Callable[[], float]

DEFAULT_CAPACITY = 1024
TRAILING_DOT = "."


def normalise_host(host: str) -> str:
    """Lower-case ``host`` without the trailing dot of a fully qualified name."""
    return host.lower().rstrip(TRAILING_DOT)


class ProxyHostStore:
    """Remembers hosts that need HOME_PROXY for ``ttl_seconds`` each."""

    def __init__(self, ttl_seconds: float, clock: Clock, capacity: int = DEFAULT_CAPACITY):
        self._ttl = ttl_seconds
        self._clock = clock
        self._capacity = capacity
        self._expiry: OrderedDict[str, float] = OrderedDict()

    def remember(self, host: str) -> None:
        """Route ``host`` through HOME_PROXY for the next ``ttl_seconds``."""
        key = normalise_host(host)
        self._expiry.pop(key, None)
        self._expiry[key] = self._clock() + self._ttl
        while len(self._expiry) > self._capacity:
            self._expiry.popitem(last=False)

    def requires_proxy(self, host: str) -> bool:
        """Whether ``host`` is remembered and its entry has not expired."""
        self._drop_expired()
        return normalise_host(host) in self._expiry

    def count(self) -> int:
        """Number of hosts currently remembered."""
        self._drop_expired()
        return len(self._expiry)

    def _drop_expired(self) -> None:
        # Entries are kept in the order they were (re-)remembered with one fixed TTL, so
        # their expiry times ascend and the expired ones are always at the front.
        now = self._clock()
        while self._expiry and next(iter(self._expiry.values())) <= now:
            self._expiry.popitem(last=False)
