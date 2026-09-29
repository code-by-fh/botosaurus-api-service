"""Learned knowledge about which sites can be served without a browser.

A site section (host, first path segment and path depth) is only trusted for
the HTTP fast path after browser renders of ``min_samples`` *different* URLs
were compared with a plain HTTP fetch and the HTTP documents contained the same
visible text. A single
mismatch marks the section as browser-only. Every verdict expires after
``ttl_seconds`` so sites that change their rendering are re-checked.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

MAX_TRACKED_SECTIONS = 10_000

Clock = Callable[[], float]


class Verdict(Enum):
    """What is known about a site section."""

    UNKNOWN = "unknown"
    HTTP_SUFFICIENT = "http_sufficient"
    BROWSER_REQUIRED = "browser_required"


@dataclass(frozen=True)
class _Entry:
    verdict: Verdict
    matched_urls: frozenset[str]
    expires_at: float


def section_key(url: str) -> str:
    """Return ``host/first-segment/depth`` for ``url``, e.g. ``shop.example/products/2``.

    Sites often render page types differently: a server-rendered listing at
    ``/products`` and a client-rendered detail page at ``/products/42`` share
    the first segment but not the depth, so they get separate verdicts.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [segment for segment in parts.path.split("/") if segment]
    first_segment = segments[0] if segments else ""
    return f"{host}/{first_segment}/{len(segments)}"


def _page_identity(url: str) -> str:
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}{parts.path.rstrip('/')}"


class VerdictStore:
    """Bounded, expiring in-memory store of per-section verdicts."""

    def __init__(self, ttl_seconds: float, min_samples: int, clock: Clock = time.monotonic):
        self._ttl = ttl_seconds
        self._min_samples = min_samples
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def get(self, key: str) -> Verdict:
        """Return the current verdict for ``key`` (``UNKNOWN`` if none or expired)."""
        entry = self._live_entry(key)
        return entry.verdict if entry else Verdict.UNKNOWN

    def record_match(self, key: str, url: str) -> None:
        """Record that HTTP delivered the same content as the browser for ``url``.

        Matches are counted per path: query variants of one page (tracking
        parameters, sorting) count once.
        """
        entry = self._live_entry(key)
        if entry and entry.verdict is Verdict.BROWSER_REQUIRED:
            return
        matched = (entry.matched_urls if entry else frozenset()) | {_page_identity(url)}
        enough = len(matched) >= self._min_samples
        verdict = Verdict.HTTP_SUFFICIENT if enough else Verdict.UNKNOWN
        expires_at = entry.expires_at if entry else self._clock() + self._ttl
        self._store(key, _Entry(verdict, matched, expires_at))

    def record_mismatch(self, key: str) -> None:
        """Record that HTTP content was incomplete; the section becomes browser-only."""
        self._store(key, _Entry(Verdict.BROWSER_REQUIRED, frozenset(), self._clock() + self._ttl))

    def counts(self) -> dict[str, int]:
        """Number of live sections per verdict, for monitoring."""
        result = {verdict.value: 0 for verdict in Verdict}
        for key in list(self._entries):
            entry = self._live_entry(key)
            if entry:
                result[entry.verdict.value] += 1
        return result

    def _live_entry(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        return entry

    def _store(self, key: str, entry: _Entry) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > MAX_TRACKED_SECTIONS:
            self._entries.popitem(last=False)
