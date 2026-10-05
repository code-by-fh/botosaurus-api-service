"""Chooses the engine for each request: plain HTTP when proven safe, else the browser.

Guarantee for ``mode="auto"``: an HTTP result is only returned when

1. the site section was verified before (see ``verifier``): browser renders and
   HTTP fetches of it contained the same visible text, and
2. this very response passes every completeness check (no challenge, no empty
   SPA mount point, enough text, all requested elements present).

Anything else is rendered in Chrome. ``mode="browser"`` skips HTTP entirely.

Browser renders use the section profile (``profiles``): learned floors that can
only make the readiness wait longer, never shorter.

``timeout`` bounds the whole request: the fast path gets a share of it
(``fast_path_seconds``), and a browser render after a failed fast path only
what is left.
"""

import asyncio
import functools
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from app.browser.blocking import BlockedResource, describe
from app.browser.page_loader import BrowserJob, BrowserPage
from app.browser.pool import BrowserPool
from app.browser.readiness import COLD, ReadinessHints
from app.content.completeness import find_incompleteness
from app.egress import EgressGateway
from app.errors import NavigationError, ProxyNotConfiguredError, TargetNotAllowedError
from app.fetch.http_fetcher import HttpFetcher, HttpFetchRequest, HttpPage
from app.log_safety import loggable_url
from app.scraping.host_limiter import HostLimiter
from app.scraping.learning import LearningStores, LearningSubject, SectionLearning
from app.scraping.profiles import SectionProfileStore
from app.scraping.verdicts import Verdict, VerdictStore, section_key
from app.scraping.verifier import HttpVerifier, VerificationSample
from app.timing import MILLISECONDS_PER_SECOND, Clock, Note, Phase, PhaseTimer
from app.url_guard import UrlGuard

log = logging.getLogger("render.scraper")

# Ready reason of a fast-path result: the section was verified, the response passed every check.
VERIFIED_HTTP_READY_REASON = "verified-http"
PROFILE_COLD = "cold"
PROFILE_LEARNED = "learned"
# The HTTP fast path has no readiness wait, so no profile applies to it.
PROFILE_NOT_APPLICABLE = "n/a"
# The fast path may use this share of the request timeout, at most FAST_PATH_MAX_SECONDS:
# a verified section answers quickly, and a failed attempt must leave the browser
# most of the budget.
FAST_PATH_TIMEOUT_SHARE = 0.3
FAST_PATH_MAX_SECONDS = 8.0

Mode = Literal["auto", "browser"]
Engine = Literal["http", "browser"]


@dataclass(frozen=True)
class ScrapeRequest:
    """A validated render request."""

    url: str
    mode: Mode
    wait_for: str | None
    selector: str | None
    timeout_seconds: float
    use_proxy: bool
    block_resources: frozenset[BlockedResource]
    timer: PhaseTimer = field(default_factory=PhaseTimer, compare=False, repr=False)

    @property
    def required_selectors(self) -> tuple[str, ...]:
        return tuple(value for value in (self.wait_for, self.selector) if value)

    @property
    def blocks_resources(self) -> bool:
        """Whether the browser was told to skip any resource kind for this request."""
        return bool(self.block_resources)

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True)
class ScrapeResult:
    """Final document plus how it was obtained.

    ``ready_reason`` says why the document was considered complete: a
    ``ReadinessEnd`` value for browser renders, ``verified-http`` for the fast path.
    ``profile`` says whether learned readiness floors applied: ``cold``,
    ``learned``, or ``n/a`` for the fast path.
    """

    html: str
    final_url: str
    engine: Engine
    stable: bool
    upstream_status: int
    ready_reason: str
    profile: str = PROFILE_NOT_APPLICABLE


@dataclass(frozen=True)
class ScraperComponents:
    """Everything the scraper orchestrates."""

    guard: UrlGuard
    egress: EgressGateway
    pool: BrowserPool
    fetcher: HttpFetcher
    verdicts: VerdictStore
    verifier: HttpVerifier
    limiter: HostLimiter
    profiles: SectionProfileStore
    clock: Clock = time.monotonic


class Scraper:
    """Entry point used by the API layer."""

    def __init__(self, components: ScraperComponents, http_first: bool):
        self._parts = components
        self._http_first = http_first

    async def scrape(self, request: ScrapeRequest) -> ScrapeResult:
        """Return the fully rendered page for ``request``.

        :raises TargetNotAllowedError: for private or non-http(s) targets.
        :raises ProxyNotConfiguredError: if ``use_proxy`` is set without a proxy.
        :raises ServiceBusyError: if capacity (global or per host) is exhausted.
        :raises NavigationError, RenderTimeoutError, TargetBlockedError, ElementNotFoundError:
            as reported by the engines.
        """
        if request.use_proxy and not self._parts.egress.has_proxy:
            raise ProxyNotConfiguredError("use_proxy is true, but HOME_PROXY is not set")
        await self._parts.guard.check(request.url)
        _log_request(request)
        async with AsyncExitStack() as host_slot:
            # Only acquiring the slot is timed, not the work done while holding it.
            with request.timer.phase(Phase.HOST_WAIT):
                await host_slot.enter_async_context(self._parts.limiter.slot(request.host))
            started = self._parts.clock()
            http_result = await self._try_http(request)
            if http_result is not None:
                return http_result
            spent = self._parts.clock() - started
            hints = self._parts.profiles.hints(section_key(request.url))
            job = self._browser_job(request, hints, request.timeout_seconds - spent)
            page = await self._parts.pool.render(job)
        return _browser_result(page, hints)

    def _browser_job(
        self, request: ScrapeRequest, hints: ReadinessHints | None, timeout_seconds: float
    ) -> BrowserJob:
        """The browser job of ``request``; ``timeout_seconds`` is what the fast path left."""
        _note_profile(request.timer, hints)
        stores = LearningStores(self._parts.profiles, self._parts.verdicts)
        verify = functools.partial(self._verify, request) if self._http_allowed(request) else None
        subject = LearningSubject(request.url, request.blocks_resources, verify)
        return BrowserJob(
            url=request.url,
            wait_for=request.wait_for,
            timeout_seconds=timeout_seconds,
            use_proxy=request.use_proxy,
            block_resources=request.block_resources,
            timer=request.timer,
            hints=hints or COLD,
            learning=SectionLearning(stores, subject),
        )

    def _http_allowed(self, request: ScrapeRequest) -> bool:
        return self._http_first and request.mode == "auto"

    async def _try_http(self, request: ScrapeRequest) -> ScrapeResult | None:
        if not self._http_allowed(request):
            log.debug("HTTP fast path disabled for %s (mode=%s)", request.url, request.mode)
            return None
        verdict = self._parts.verdicts.get(section_key(request.url))
        if verdict is not Verdict.HTTP_SUFFICIENT:
            log.debug("Section verdict for %s is %s, using browser", request.url, verdict)
            return None
        with request.timer.phase(Phase.HTTP):
            return await self._fetch_verified(request)

    async def _fetch_verified(self, request: ScrapeRequest) -> ScrapeResult | None:
        page = await self._fetch_fast(request)
        if page is None:
            return None
        if not self._stays_in_verified_sections(page.final_url):
            log.info(
                "HTTP result for %s left the verified section, using browser",
                loggable_url(request.url),
            )
            return None
        reason = await asyncio.to_thread(_rejection_reason, page, request.required_selectors)
        if reason:
            log.info(
                "HTTP result for %s rejected (%s), using browser", loggable_url(request.url), reason
            )
            self._parts.verdicts.record_mismatch(section_key(request.url))
            return None
        log.debug("HTTP fast path succeeded for %s", request.url)
        return ScrapeResult(
            page.html, page.final_url, "http", True, page.status, VERIFIED_HTTP_READY_REASON
        )

    async def _fetch_fast(self, request: ScrapeRequest) -> HttpPage | None:
        """The fast-path response, or ``None`` if the fetch failed and the browser must render."""
        fetch = self._fetch_request(request, fast_path_seconds(request.timeout_seconds))
        try:
            return await self._parts.fetcher.fetch(fetch)
        except (NavigationError, TargetNotAllowedError) as exc:
            # A redirect to a forbidden target says nothing about the section, so it is
            # no mismatch; the browser's egress guard still refuses that target.
            log.info(
                "HTTP fast path failed for %s, using browser: %s",
                loggable_url(request.url),
                exc.message,
            )
            return None

    def _stays_in_verified_sections(self, final_url: str) -> bool:
        return self._parts.verdicts.get(section_key(final_url)) is Verdict.HTTP_SUFFICIENT

    def _verify(self, request: ScrapeRequest, page: BrowserPage) -> None:
        """Verify ``page`` over HTTP; ``SectionLearning`` calls it after a clean late watch."""
        if not self._parts.verdicts.wants_sample(section_key(request.url), request.url):
            return
        sample = VerificationSample(
            fetch=self._fetch_request(request, request.timeout_seconds),
            host=request.host,
            required_selectors=request.required_selectors,
            browser_page=page,
        )
        self._parts.verifier.schedule(sample)

    def _fetch_request(self, request: ScrapeRequest, timeout_seconds: float) -> HttpFetchRequest:
        proxy = self._parts.egress.url_for(request.use_proxy)
        if request.use_proxy:
            log.info(
                "HTTP fetch for %s via HOME_PROXY egress (%s)", loggable_url(request.url), proxy
            )
        else:
            log.debug("HTTP fetch for %s via direct egress (%s)", request.url, proxy)
        return HttpFetchRequest(request.url, timeout_seconds, proxy)


def fast_path_seconds(timeout_seconds: float) -> float:
    """The share of a request's ``timeout_seconds`` the HTTP fast path may use."""
    return min(FAST_PATH_MAX_SECONDS, FAST_PATH_TIMEOUT_SHARE * timeout_seconds)


def _log_request(request: ScrapeRequest) -> None:
    blocked = describe(request.block_resources)
    request.timer.note(Note.BLOCKED, blocked)
    log.debug(
        "Scraping %s | mode=%s use_proxy=%s block_resources=%s timeout=%ss",
        request.url,
        request.mode,
        request.use_proxy,
        blocked,
        request.timeout_seconds,
    )
    if request.use_proxy:
        log.info("Request for %s routed via HOME_PROXY", loggable_url(request.url))


def _note_profile(timer: PhaseTimer, hints: ReadinessHints | None) -> None:
    timer.note(Note.PROFILE, PROFILE_LEARNED if hints else PROFILE_COLD)
    min_ready = hints.min_ready_seconds if hints else 0.0
    timer.note(Note.MIN_READY_MS, round(min_ready * MILLISECONDS_PER_SECOND))


def _browser_result(page: BrowserPage, hints: ReadinessHints | None) -> ScrapeResult:
    return ScrapeResult(
        html=page.html,
        final_url=page.final_url,
        engine="browser",
        stable=page.stable,
        upstream_status=page.status,
        ready_reason=page.ready_reason,
        profile=PROFILE_LEARNED if hints else PROFILE_COLD,
    )


def _rejection_reason(page, required_selectors: tuple[str, ...]) -> str | None:
    if not page.is_html_document:
        return f"status {page.status}, content type '{page.content_type}'"
    return find_incompleteness(page.html, required_selectors)
