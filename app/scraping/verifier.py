"""Background check whether a site section can be served by plain HTTP.

After a browser render of a section with an unknown verdict has passed its
late-content watch without growing (``scraping.learning``), the same URL is
fetched once without a browser (after a short random pause, so the two
requests do not arrive at the same instant) and both visible texts are
compared. Only if the HTTP document passes every completeness check, contains
at least ``min_text_coverage`` of the browser's words and text length, and
contains every number the browser showed, does it count as a match.
"""

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property

from app.browser.page_loader import BrowserPage
from app.content.completeness import find_incompleteness
from app.content.text import compare_texts, parse_html, visible_text
from app.errors import ServiceBusyError, ServiceError
from app.fetch.http_fetcher import HttpFetcher, HttpFetchRequest
from app.log_safety import loggable_url
from app.scraping.host_limiter import HostLimiter
from app.scraping.verdicts import VerdictStore, section_key

log = logging.getLogger("render.verify")

MAX_CONCURRENT_VERIFICATIONS = 4
PAUSE_RANGE_SECONDS = (1.0, 3.0)

PauseSource = Callable[[], float]


def random_pause() -> float:
    """Pick a human-looking delay between the browser render and the HTTP fetch."""
    return random.uniform(*PAUSE_RANGE_SECONDS)


@dataclass(frozen=True)
class VerificationSample:
    """A finished browser render that serves as the reference."""

    fetch: HttpFetchRequest
    host: str
    required_selectors: tuple[str, ...]
    browser_page: BrowserPage

    @cached_property
    def browser_text(self) -> str:
        """Visible text of the browser render, parsed once for comparison and recording."""
        return visible_text(parse_html(self.browser_page.html))


def http_matches_browser(http_html: str, sample: VerificationSample, min_coverage: float) -> bool:
    """True if ``http_html`` is complete and covers the browser's visible text."""
    reason = find_incompleteness(http_html, sample.required_selectors)
    if reason:
        log.info("HTTP result for %s rejected: %s", loggable_url(sample.fetch.url), reason)
        return False
    comparison = compare_texts(sample.browser_text, visible_text(parse_html(http_html)))
    log.info(
        "HTTP vs browser for %s: coverage %.2f, length %.2f, missing numbers %d",
        loggable_url(sample.fetch.url),
        comparison.coverage,
        comparison.length_ratio,
        len(comparison.missing_numbers),
    )
    return comparison.matches(min_coverage)


@dataclass(frozen=True)
class VerifierCollaborators:
    """Components the verifier works with."""

    fetcher: HttpFetcher
    verdicts: VerdictStore
    limiter: HostLimiter


@dataclass(frozen=True)
class VerifierPolicy:
    """How strict the comparison is and how long to pause before fetching."""

    min_coverage: float
    pause: PauseSource = random_pause


class HttpVerifier:
    """Schedules and runs verification fetches, at most one per section at a time."""

    def __init__(self, collaborators: VerifierCollaborators, policy: VerifierPolicy):
        self._fetcher = collaborators.fetcher
        self._verdicts = collaborators.verdicts
        self._limiter = collaborators.limiter
        self._policy = policy
        self._running: dict[str, asyncio.Task[None]] = {}

    def schedule(self, sample: VerificationSample) -> None:
        """Start a background verification unless one is running or capacity is used up."""
        key = section_key(sample.fetch.url)
        if key in self._running or len(self._running) >= MAX_CONCURRENT_VERIFICATIONS:
            return
        task = asyncio.create_task(self._verify(key, sample))
        self._running[key] = task
        task.add_done_callback(lambda _: self._running.pop(key, None))

    async def drain(self) -> None:
        """Wait for all running verifications (used on shutdown and in tests)."""
        await asyncio.gather(*self._running.values(), return_exceptions=True)

    async def _verify(self, key: str, sample: VerificationSample) -> None:
        # Top level of a background task: nothing awaits it, so every failure
        # has to be logged here or it would vanish.
        try:
            await asyncio.sleep(self._policy.pause())
            matched = await self._compare(sample)
        except ServiceBusyError:
            log.info("Verification of %s skipped, host is busy", loggable_url(sample.fetch.url))
            return
        except Exception:
            log.error("Verification of %s crashed", loggable_url(sample.fetch.url), exc_info=True)
            return
        if matched:
            self._verdicts.record_match(key, sample.fetch.url, sample.browser_text)
        else:
            self._verdicts.record_mismatch(key)
        log.info("Section %s verdict is now %s", key, self._verdicts.get(key).value)

    async def _compare(self, sample: VerificationSample) -> bool:
        try:
            async with self._limiter.slot(sample.host):
                page = await self._fetcher.fetch(sample.fetch)
        except ServiceBusyError:
            raise
        except ServiceError as exc:
            log.info(
                "Verification fetch for %s failed: %s",
                loggable_url(sample.fetch.url),
                exc.message,
            )
            return False
        if not page.is_html_document:
            return False
        return await asyncio.to_thread(
            http_matches_browser, page.html, sample, self._policy.min_coverage
        )
