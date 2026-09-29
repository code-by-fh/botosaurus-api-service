"""Wires all components together and owns their lifecycle."""

from collections.abc import Callable
from dataclasses import dataclass, field

from app.browser.pool import BrowserPool
from app.browser.session import Launcher, SessionFactory, launch_chrome
from app.browser.worker import BrowserWorker
from app.config import Settings
from app.egress import EgressGateway
from app.fetch.http_fetcher import HttpFetcher
from app.scraping.host_limiter import HostLimiter
from app.scraping.scraper import Scraper, ScraperComponents
from app.scraping.verdicts import VerdictStore
from app.scraping.verifier import (
    HttpVerifier,
    PauseSource,
    VerifierCollaborators,
    VerifierPolicy,
    random_pause,
)
from app.url_guard import Resolver, UrlGuard, resolve_host

FetcherFactory = Callable[[UrlGuard, Settings], HttpFetcher]


def _default_fetcher(guard: UrlGuard, settings: Settings) -> HttpFetcher:
    return HttpFetcher(guard, settings.browser.locale, settings.http_first.max_response_bytes)


@dataclass(frozen=True)
class Adapters:
    """Connections to the outside world (and the randomness source); replaced in tests."""

    launcher: Launcher = launch_chrome
    fetcher_factory: FetcherFactory = _default_fetcher
    resolver: Resolver = field(default=resolve_host)
    pause: PauseSource = field(default=random_pause)


@dataclass(frozen=True)
class Runtime:
    """Started components the API layer uses."""

    scraper: Scraper
    egress: EgressGateway
    pool: BrowserPool
    fetcher: HttpFetcher
    verdicts: VerdictStore
    verifier: HttpVerifier

    async def close(self) -> None:
        """Stop background verification, browsers, HTTP connections and egress proxies."""
        await self.verifier.drain()
        await self.pool.shutdown()
        await self.fetcher.close()
        await self.egress.close()


def _verifier(settings: Settings, parts: VerifierCollaborators, pause: PauseSource) -> HttpVerifier:
    policy = VerifierPolicy(min_coverage=settings.http_first.min_text_coverage, pause=pause)
    return HttpVerifier(parts, policy)


async def start_runtime(settings: Settings, adapters: Adapters | None = None) -> Runtime:
    """Create every component and launch the browser pool.

    :param adapters: outside-world connections; the production ones when omitted.
    """
    adapters = adapters or Adapters()
    guard = UrlGuard(settings.allow_private_targets, adapters.resolver)
    fetcher = adapters.fetcher_factory(guard, settings)
    verdicts = VerdictStore(
        settings.http_first.verdict_ttl_seconds, settings.http_first.verdict_min_samples
    )
    limiter = HostLimiter(settings.queue.max_per_host, settings.queue.timeout_seconds)
    verifier = _verifier(
        settings, VerifierCollaborators(fetcher, verdicts, limiter), adapters.pause
    )
    egress = EgressGateway(guard, settings.browser.proxy_url)
    await egress.start()
    pool = await _start_pool(settings, SessionFactory(settings.browser, adapters.launcher, egress))
    components = ScraperComponents(guard, egress, pool, fetcher, verdicts, verifier, limiter)
    scraper = Scraper(components, settings.http_first.enabled)
    return Runtime(scraper, egress, pool, fetcher, verdicts, verifier)


async def _start_pool(settings: Settings, factory: SessionFactory) -> BrowserPool:
    workers = [BrowserWorker(factory) for _ in range(settings.browser.worker_count)]
    pool = BrowserPool(workers, settings.queue)
    await pool.start()
    return pool
