"""Chooses the engine for each request: plain HTTP when proven safe, else the browser.

Guarantee for ``mode="auto"``: an HTTP result is only returned when

1. the site section was verified before (see ``verifier``): browser renders and
   HTTP fetches of it contained the same visible text, and
2. this very response passes every completeness check (no challenge, no empty
   SPA mount point, enough text, all requested elements present).

Anything else is rendered in Chrome. ``mode="browser"`` skips HTTP entirely.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from app.browser.page_loader import BrowserJob, BrowserPage
from app.browser.pool import BrowserPool
from app.content.completeness import find_incompleteness
from app.egress import EgressGateway
from app.errors import NavigationError, ProxyNotConfiguredError
from app.fetch.http_fetcher import HttpFetcher, HttpFetchRequest
from app.scraping.host_limiter import HostLimiter
from app.scraping.verdicts import Verdict, VerdictStore, section_key
from app.scraping.verifier import HttpVerifier, VerificationSample
from app.url_guard import UrlGuard

log = logging.getLogger("render.scraper")

HTTP_OK = 200

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
    block_resources: bool
    idle_timeout_seconds: float | None = None

    @property
    def required_selectors(self) -> tuple[str, ...]:
        return tuple(value for value in (self.wait_for, self.selector) if value)

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True)
class ScrapeResult:
    """Final document plus how it was obtained."""

    html: str
    final_url: str
    engine: Engine
    stable: bool
    upstream_status: int


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
        log.debug(
            "Scraping %s | mode=%s use_proxy=%s block_resources=%s timeout=%ss",
            request.url,
            request.mode,
            request.use_proxy,
            request.block_resources,
            request.timeout_seconds,
        )
        if request.use_proxy:
            log.info("Request for %s routed via HOME_PROXY", request.url)
        async with self._parts.limiter.slot(request.host):
            http_result = await self._try_http(request)
            if http_result is not None:
                return http_result
            page = await self._parts.pool.render(_browser_job(request))
        self._maybe_verify(request, page)
        return ScrapeResult(page.html, page.final_url, "browser", page.stable, page.status)

    def _http_allowed(self, request: ScrapeRequest) -> bool:
        return self._http_first and request.mode == "auto"

    async def _try_http(self, request: ScrapeRequest) -> ScrapeResult | None:
        key = section_key(request.url)
        if not self._http_allowed(request):
            log.debug("HTTP fast path disabled for %s (mode=%s)", request.url, request.mode)
            return None
        verdict = self._parts.verdicts.get(key)
        if verdict is not Verdict.HTTP_SUFFICIENT:
            log.debug("Section verdict for %s is %s, using browser", request.url, verdict)
            return None
        try:
            page = await self._parts.fetcher.fetch(self._fetch_request(request))
        except NavigationError as exc:
            log.info("HTTP fast path failed for %s, using browser: %s", request.url, exc.message)
            return None
        if not self._stays_in_verified_sections(page.final_url):
            log.info("HTTP result for %s left the verified section, using browser", request.url)
            return None
        reason = await asyncio.to_thread(_rejection_reason, page, request.required_selectors)
        if reason:
            log.info("HTTP result for %s rejected (%s), using browser", request.url, reason)
            self._parts.verdicts.record_mismatch(key)
            return None
        log.debug("HTTP fast path succeeded for %s", request.url)
        return ScrapeResult(page.html, page.final_url, "http", True, page.status)

    def _stays_in_verified_sections(self, final_url: str) -> bool:
        return self._parts.verdicts.get(section_key(final_url)) is Verdict.HTTP_SUFFICIENT

    def _maybe_verify(self, request: ScrapeRequest, page: BrowserPage) -> None:
        known = self._parts.verdicts.get(section_key(request.url)) is not Verdict.UNKNOWN
        usable_reference = page.stable and page.status == HTTP_OK
        if known or not usable_reference or not self._http_allowed(request):
            return
        sample = VerificationSample(
            fetch=self._fetch_request(request),
            host=request.host,
            required_selectors=request.required_selectors,
            browser_page=page,
        )
        self._parts.verifier.schedule(sample)

    def _fetch_request(self, request: ScrapeRequest) -> HttpFetchRequest:
        proxy = self._parts.egress.url_for(request.use_proxy)
        if request.use_proxy:
            log.info("HTTP fetch for %s via HOME_PROXY egress (%s)", request.url, proxy)
        else:
            log.debug("HTTP fetch for %s via direct egress (%s)", request.url, proxy)
        return HttpFetchRequest(request.url, request.timeout_seconds, proxy)


def _rejection_reason(page, required_selectors: tuple[str, ...]) -> str | None:
    if not page.is_html_document:
        return f"status {page.status}, content type '{page.content_type}'"
    return find_incompleteness(page.html, required_selectors)


def _browser_job(request: ScrapeRequest) -> BrowserJob:
    return BrowserJob(
        url=request.url,
        wait_for=request.wait_for,
        timeout_seconds=request.timeout_seconds,
        use_proxy=request.use_proxy,
        block_resources=request.block_resources,
        idle_timeout_seconds=request.idle_timeout_seconds,
    )
