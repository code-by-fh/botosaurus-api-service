"""Wires all components together and owns their lifecycle."""

import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from app.browser.blocking import default_tracker_domains
from app.browser.page_loader import LoadServices
from app.browser.pool import BrowserPool
from app.browser.session import Launcher, SessionFactory, launch_chrome
from app.browser.worker import BrowserWorker
from app.config import Settings
from app.egress import EgressGateway
from app.fetch.http_fetcher import HttpFetcher
from app.scraping.clearance import ClearanceStore
from app.scraping.host_limiter import HostLimiter
from app.scraping.profiles import ChanceSource, ProfilePolicy, SectionProfileStore
from app.scraping.proxy_hosts import ProxyHostStore
from app.scraping.scraper import EscalationPolicy, Scraper, ScraperComponents, ScraperPolicy
from app.scraping.verdicts import VerdictPolicy, VerdictStore
from app.scraping.verifier import (
    HttpVerifier,
    PauseSource,
    VerifierCollaborators,
    VerifierPolicy,
    random_pause,
)
from app.timing import Clock
from app.url_guard import Resolver, UrlGuard, resolve_host

log = logging.getLogger("render.runtime")

FetcherFactory = Callable[[UrlGuard, Settings], HttpFetcher]
CloseStep = Callable[[], Awaitable[None]]


async def _close_all(steps: Sequence[CloseStep]) -> None:
    """Run every step even if an earlier one fails, so no browser or socket is left behind.

    :raises Exception: the first failure, after all steps ran.
    """
    first_error: Exception | None = None
    for step in steps:
        try:
            await step()
        except Exception as exc:
            log.error("Shutdown step %s failed", step.__qualname__, exc_info=True)
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _default_fetcher(guard: UrlGuard, settings: Settings) -> HttpFetcher:
    return HttpFetcher(guard, settings.browser.locale, settings.http_first.max_response_bytes)


@dataclass(frozen=True)
class Adapters:
    """Connections to the outside world (plus randomness and clocks); replaced in tests.

    ``clock`` is the monotonic clock renders are timed on and profiles expire by;
    ``chance`` decides which renders are observed for late content.
    """

    launcher: Launcher = launch_chrome
    fetcher_factory: FetcherFactory = _default_fetcher
    resolver: Resolver = field(default=resolve_host)
    pause: PauseSource = field(default=random_pause)
    wall_clock: Callable[[], float] = field(default=time.time)
    clock: Clock = field(default=time.monotonic)
    chance: ChanceSource = field(default=random.random)


@dataclass(frozen=True)
class Runtime:
    """Started components the API layer uses."""

    scraper: Scraper
    egress: EgressGateway
    pool: BrowserPool
    fetcher: HttpFetcher
    verdicts: VerdictStore
    verifier: HttpVerifier
    clearance: ClearanceStore | None
    profiles: SectionProfileStore
    proxy_hosts: ProxyHostStore

    async def close(self) -> None:
        """Stop background verification, observations, browsers, HTTP and egress proxies.

        Every part is closed even if an earlier one fails.

        :raises Exception: the first failure, after all parts were closed.
        """
        steps = (self.verifier.drain, self.pool.shutdown, self.fetcher.close, self.egress.close)
        await _close_all(steps)


def _verifier(settings: Settings, parts: VerifierCollaborators, pause: PauseSource) -> HttpVerifier:
    policy = VerifierPolicy(min_coverage=settings.http_first.min_text_coverage, pause=pause)
    return HttpVerifier(parts, policy)


async def start_runtime(settings: Settings, adapters: Adapters | None = None) -> Runtime:
    """Create every component and launch the browser pool.

    If startup fails part-way, whatever was already started is stopped again.

    :param adapters: outside-world connections; the production ones when omitted.
    :raises ConfigError: if the shipped tracker domain list is missing or broken.
    """
    # Read before anything starts, so a broken list stops the service instead of the
    # first browser render.
    default_tracker_domains()
    adapters = adapters or Adapters()
    guard = UrlGuard(settings.allow_private_targets, adapters.resolver)
    fetcher = adapters.fetcher_factory(guard, settings)
    egress = EgressGateway(guard, settings.browser.proxy_url)
    try:
        await egress.start()
        return await _assemble(settings, adapters, (guard, fetcher, egress))
    except BaseException:
        # _close_all logged every failure of the rollback; the startup error is the one
        # to report.
        with contextlib.suppress(Exception):
            await _close_all((egress.close, fetcher.close))
        raise


async def _assemble(
    settings: Settings, adapters: Adapters, started: tuple[UrlGuard, HttpFetcher, EgressGateway]
) -> Runtime:
    guard, fetcher, egress = started
    verdicts = _verdict_store(settings, adapters)
    limiter = HostLimiter(settings.queue.max_per_host, settings.queue.timeout_seconds)
    verifier = _verifier(
        settings, VerifierCollaborators(fetcher, verdicts, limiter), adapters.pause
    )
    clearance = _clearance_store(settings, adapters)
    factory = SessionFactory(settings.browser, adapters.launcher, egress)
    services = LoadServices(clearance, adapters.clock, settings.http_first.max_response_bytes)
    pool = await _start_pool(settings, factory, services)
    profiles = _profile_store(settings, adapters)
    proxy_hosts = ProxyHostStore(settings.auto_proxy.ttl_seconds, adapters.clock)
    scraping = (verdicts, verifier, limiter, profiles, proxy_hosts)
    components = ScraperComponents(guard, egress, pool, fetcher, *scraping, adapters.clock)
    scraper = Scraper(components, _scraper_policy(settings))
    return Runtime(
        scraper, egress, pool, fetcher, verdicts, verifier, clearance, profiles, proxy_hosts
    )


def _scraper_policy(settings: Settings) -> ScraperPolicy:
    config = settings.auto_proxy
    escalation = EscalationPolicy(config.enabled, config.challenge_seconds)
    return ScraperPolicy(settings.http_first.enabled, escalation)


def _verdict_store(settings: Settings, adapters: Adapters) -> VerdictStore:
    config = settings.http_first
    policy = VerdictPolicy(
        config.verdict_ttl_seconds, config.verdict_min_samples, config.same_page_interval_seconds
    )
    return VerdictStore(policy, adapters.clock)


def _profile_store(settings: Settings, adapters: Adapters) -> SectionProfileStore:
    config = settings.profiles
    policy = ProfilePolicy(config.ttl_seconds, config.observe_seconds, config.sample_rate)
    return SectionProfileStore(policy, adapters.clock, adapters.chance)


def _clearance_store(settings: Settings, adapters: Adapters) -> ClearanceStore | None:
    if not settings.clearance.enabled:
        return None
    return ClearanceStore(settings.clearance.max_age_seconds, adapters.wall_clock)


async def _start_pool(
    settings: Settings, factory: SessionFactory, services: LoadServices
) -> BrowserPool:
    workers = [BrowserWorker(factory, services) for _ in range(settings.browser.worker_count)]
    pool = BrowserPool(workers, settings.queue)
    try:
        await pool.start()
    except BaseException:
        # Browsers launched before the failing one would otherwise outlive the service.
        await pool.shutdown()
        raise
    return pool
