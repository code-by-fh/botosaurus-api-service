"""Learned knowledge about which sites can be served without a browser.

A site section (host, first path segment and path depth) is trusted for the HTTP
fast path once plain HTTP fetches reproduced the visible text of clean browser
renders (see ``verifier``) in ``min_samples`` matches that are either

- of *different* pages: distinct normalised paths that also showed distinct
  browser text, so spelling variants and redirects to one page count once; or
- of the *same* page, at least ``same_page_interval_seconds`` apart: evidence
  that a single-page section (a homepage) is stably server-rendered over time.

A single mismatch marks the section as browser-only. Every verdict expires after
``ttl_seconds`` so sites that change their rendering are re-checked.
"""

import hashlib
import re
import string
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

MAX_TRACKED_SECTIONS = 10_000
# Bounds memory per section. Matches beyond it are ignored, which can only withhold a verdict.
MAX_MATCHES_PER_SECTION = 32
TEXT_HASH_LENGTH = 16
HEX_BASE = 16
UNRESERVED_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-._~")
PERCENT_ESCAPE_PATTERN = re.compile(r"%([0-9A-Fa-f]{2})")

Clock = Callable[[], float]


class Verdict(Enum):
    """What is known about a site section."""

    UNKNOWN = "unknown"
    HTTP_SUFFICIENT = "http_sufficient"
    BROWSER_REQUIRED = "browser_required"


@dataclass(frozen=True)
class VerdictPolicy:
    """How long verdicts live and how much evidence unlocks the HTTP fast path."""

    ttl_seconds: float
    min_samples: int
    same_page_interval_seconds: float


@dataclass(frozen=True)
class _Match:
    identity: str
    text_hash: str
    at: float


@dataclass(frozen=True)
class _Entry:
    verdict: Verdict
    matches: tuple[_Match, ...]
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


def _decode_unreserved(path: str) -> str:
    def decode(escape: re.Match[str]) -> str:
        character = chr(int(escape.group(1), HEX_BASE))
        return character if character in UNRESERVED_CHARACTERS else escape.group(0)

    return PERCENT_ESCAPE_PATTERN.sub(decode, path)


def _page_identity(url: str) -> str:
    """Host and normalised path of ``url``: the page a match is evidence for.

    Percent-escaped unreserved characters are decoded, ``;`` path parameters
    (session ids) and empty segments dropped and the path is lower-cased. Lower-casing
    can only merge pages that a case-sensitive server tells apart, so it can only
    withhold a verdict, never grant one too early.
    """
    parts = urlsplit(url)
    segments = (segment.split(";", 1)[0] for segment in _decode_unreserved(parts.path).split("/"))
    path = "/".join(segment for segment in segments if segment).lower()
    return f"{(parts.hostname or '').lower()}/{path}"


def _text_hash(text: str) -> str:
    normalised = " ".join(text.lower().split())
    return hashlib.sha256(normalised.encode()).hexdigest()[:TEXT_HASH_LENGTH]


def _distinct_pages(matches: Iterable[_Match]) -> int:
    """Number of pages, joining matches that share an identity or a browser text."""
    groups: list[set[str]] = []
    for match in matches:
        keys = {f"page:{match.identity}", f"text:{match.text_hash}"}
        joined = [group for group in groups if group & keys]
        groups = [group for group in groups if not group & keys]
        groups.append(keys.union(*joined))
    return len(groups)


def _spaced_matches(times: list[float], interval: float) -> int:
    """How many of the sorted ``times`` are at least ``interval`` after the previous counted one."""
    count = 0
    last: float | None = None
    for at in times:
        if last is None or at - last >= interval:
            count += 1
            last = at
    return count


class VerdictStore:
    """Bounded, expiring in-memory store of per-section verdicts."""

    def __init__(self, policy: VerdictPolicy, clock: Clock = time.monotonic):
        self._policy = policy
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def get(self, key: str) -> Verdict:
        """Return the current verdict for ``key`` (``UNKNOWN`` if none or expired)."""
        entry = self._live_entry(key)
        return entry.verdict if entry else Verdict.UNKNOWN

    def wants_sample(self, key: str, url: str) -> bool:
        """Whether a verification of ``url`` could still add evidence to section ``key``.

        Not if the section has a verdict, or if the same page matched within the
        same-page interval, because such a match would not count.
        """
        entry = self._live_entry(key)
        if entry is None:
            return True
        if entry.verdict is not Verdict.UNKNOWN:
            return False
        identity = _page_identity(url)
        recent = self._clock() - self._policy.same_page_interval_seconds
        return not any(match.identity == identity and match.at > recent for match in entry.matches)

    def record_match(self, key: str, url: str, browser_text: str) -> None:
        """Record that HTTP delivered the same content as the browser render of ``url``.

        :param browser_text: the visible text of that render; renders with the same
            text count as one page, whatever their URLs.
        """
        entry = self._live_entry(key)
        if entry and entry.verdict is Verdict.BROWSER_REQUIRED:
            return
        matches = entry.matches if entry else ()
        if len(matches) >= MAX_MATCHES_PER_SECTION:
            return
        matches += (_Match(_page_identity(url), _text_hash(browser_text), self._clock()),)
        verdict = Verdict.HTTP_SUFFICIENT if self._enough(matches) else Verdict.UNKNOWN
        expires_at = entry.expires_at if entry else self._clock() + self._policy.ttl_seconds
        self._store(key, _Entry(verdict, matches, expires_at))

    def _enough(self, matches: tuple[_Match, ...]) -> bool:
        needed = self._policy.min_samples
        if _distinct_pages(matches) >= needed:
            return True
        interval = self._policy.same_page_interval_seconds
        identities = {match.identity for match in matches}
        return any(
            _spaced_matches([match.at for match in matches if match.identity == page], interval)
            >= needed
            for page in identities
        )

    def record_mismatch(self, key: str) -> None:
        """Record that HTTP content was incomplete; the section becomes browser-only."""
        expires_at = self._clock() + self._policy.ttl_seconds
        self._store(key, _Entry(Verdict.BROWSER_REQUIRED, (), expires_at))

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
