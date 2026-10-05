"""Counts the network requests of a tab that can still change the page content.

Fed by CDP ``Network`` events (``requestWillBeSent``, ``responseReceived``,
``loadingFinished``, ``loadingFailed``), the same signal prerender.io uses: a
page is not done while a request that may carry content is still open. Unlike
the Resource Timing buffer, these events also show requests that are still in
flight and are not capped at 250 entries.

Only requests that can deliver content count: documents (main frame and
iframes), scripts, XHR and fetch. Everything else is ignored, and so is

- every request to a host on the shipped tracker list
  (``blocking.default_tracker_domains``), because analytics and beacons keep
  firing long after the content is there;
- an event stream from the moment its response arrives;
- a request open for ``LONG_POLL_SECONDS`` or longer, which is treated as a long
  poll. Ignoring it is safe: if its answer changes the page, the DOM growth
  observer in ``readiness`` still holds the render back.

``Network.enable`` (sent by zendriver when the first handler is registered)
does not change anything the page can observe.
"""

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from zendriver import cdp

from app.browser.activity import ActivityLog

LONG_POLL_SECONDS = 5.0
CONTENT_RESOURCE_TYPES = frozenset(
    {
        cdp.network.ResourceType.DOCUMENT,
        cdp.network.ResourceType.XHR,
        cdp.network.ResourceType.FETCH,
        cdp.network.ResourceType.SCRIPT,
    }
)


class EventTab(Protocol):
    """The part of a zendriver tab that delivers CDP events."""

    def add_handler(self, event_type: Any, handler: Any) -> None: ...

    def remove_handlers(self, event_type: Any = None, handler: Any = None) -> None: ...


@dataclass(frozen=True)
class NetworkSnapshot:
    """Content requests still open, and requests ignored so far (for the timing log)."""

    inflight: int
    ignored: int


IDLE_NETWORK = NetworkSnapshot(inflight=0, ignored=0)


class InflightTracker:
    """In-flight content requests of one tab during one render.

    Handlers are coroutines so that zendriver runs them on the event loop (it
    moves plain functions to a thread); all state is touched from the loop only.
    """

    def __init__(self, tracker_domains: frozenset[str], activity: ActivityLog):
        """:param tracker_domains: hosts whose requests (and subdomains') never count.
        :param activity: log that receives the start and finish of each counted request.
        """
        self._tracker_domains = tracker_domains
        self._activity = activity
        self._open: dict[str, float] = {}
        self._ignored: set[str] = set()
        self._handlers = {
            cdp.network.RequestWillBeSent: self._on_request,
            cdp.network.ResponseReceived: self._on_response,
            cdp.network.LoadingFinished: self._on_done,
            cdp.network.LoadingFailed: self._on_done,
        }

    def attach(self, tab: EventTab) -> None:
        """Start listening; must happen before navigation to see the document request."""
        for event_type, handler in self._handlers.items():
            tab.add_handler(event_type, handler)

    def detach(self, tab: EventTab) -> None:
        """Stop listening."""
        for event_type, handler in self._handlers.items():
            tab.remove_handlers(event_type, handler)

    def snapshot(self) -> NetworkSnapshot:
        """Open content requests younger than ``LONG_POLL_SECONDS``, plus the ignored count."""
        now = self._activity.now()
        inflight = sum(1 for started in self._open.values() if now - started < LONG_POLL_SECONDS)
        long_polls = len(self._open) - inflight
        return NetworkSnapshot(inflight=inflight, ignored=len(self._ignored) + long_polls)

    async def _on_request(self, event: cdp.network.RequestWillBeSent) -> None:
        request_id = str(event.request_id)
        if not self._carries_content(event):
            # A redirect hop to a tracker turns an already counted request into an ignored one.
            self._open.pop(request_id, None)
            self._ignored.add(request_id)
            return
        started = self._activity.record(idle_before=self.snapshot().inflight == 0)
        # A redirect hop reuses the request id; the request keeps its original start.
        self._open.setdefault(request_id, started)

    async def _on_response(self, event: cdp.network.ResponseReceived) -> None:
        if event.type_ is cdp.network.ResourceType.EVENT_SOURCE:
            request_id = str(event.request_id)
            self._open.pop(request_id, None)
            self._ignored.add(request_id)

    async def _on_done(
        self, event: cdp.network.LoadingFinished | cdp.network.LoadingFailed
    ) -> None:
        request_id = str(event.request_id)
        started = self._open.pop(request_id, None)
        if started is None:
            return
        if self._activity.now() - started >= LONG_POLL_SECONDS:
            self._ignored.add(request_id)
            return
        # The request was open since its start, so the gap before its end was not idle.
        self._activity.record(idle_before=False)

    def _carries_content(self, event: cdp.network.RequestWillBeSent) -> bool:
        # A request without a type is counted: when in doubt, wait for it (bounded by
        # LONG_POLL_SECONDS).
        if event.type_ is not None and event.type_ not in CONTENT_RESOURCE_TYPES:
            return False
        return not self._is_tracker(urlsplit(event.request.url).hostname or "")

    def _is_tracker(self, host: str) -> bool:
        labels = host.lower().split(".")
        suffixes = (".".join(labels[index:]) for index in range(len(labels)))
        return any(suffix in self._tracker_domains for suffix in suffixes)
