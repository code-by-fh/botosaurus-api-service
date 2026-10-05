"""Per-section readiness learning, end to end through the real runtime with fake Chrome."""

import asyncio
import logging
import time
from collections.abc import Callable

import pytest
from zendriver.core.connection import ProtocolException

from app.browser.blocking import BlockedResource
from app.config import DEFAULT_VERDICT_SAME_PAGE_INTERVAL_SECONDS
from app.runtime import Adapters, Runtime, start_runtime
from app.scraping.profiles import MIN_READY_MARGIN_SECONDS, PROFILE_OBSERVE_FIRST_RENDERS
from app.scraping.scraper import ScrapeRequest, ScrapeResult
from app.scraping.verdicts import Verdict, section_key
from app.timing import Clock, PhaseTimer
from tests.fakes import (
    SERVER_RENDERED_HTML,
    FakeHttpFetcher,
    FakeLauncher,
    FakePage,
    ManualClock,
    article_html,
    http_page,
    make_settings,
    public_resolver,
    stable_probe,
)

# Two pages of one section (host, first segment, depth) and one page elsewhere.
FIRST_QUOTES_URL = "https://quotes.example/js-delayed/page-1"
SECOND_QUOTES_URL = "https://quotes.example/js-delayed/page-2"
THIRD_QUOTES_URL = "https://quotes.example/js-delayed/page-3"
OTHER_SITE_URL = "https://other.example/article"
PROBE_SECONDS = 0.125
# Like quotes.toscrape.com/js-delayed/: silent for about 10 s, then the quotes appear.
SILENT_PROBES = 80
LATE_CONTENT_SECONDS = (SILENT_PROBES + 1) * PROBE_SECONDS
QUOTES_HTML = SERVER_RENDERED_HTML.replace(
    "</main>",
    "<p class='quote'>The world as we have created it is a process of our thinking.</p></main>",
)
OBSERVE_SECONDS = "10"
LEARNING_TIMEOUT_SECONDS = 20.0
NEVER_SAMPLED = 1.0
SAME_PAGE_INTERVAL_SECONDS = DEFAULT_VERDICT_SAME_PAGE_INTERVAL_SECONDS


def silent_timer_page(**changes) -> FakePage:
    values = {
        "html": SERVER_RENDERED_HTML,
        "probes": [stable_probe(growth=0)] * SILENT_PROBES + [stable_probe(growth=1)],
        "late_html": QUOTES_HTML,
        "late_html_after_probe": SILENT_PROBES,
        "probe_seconds": PROBE_SECONDS,
    }
    values.update(changes)
    return FakePage(**values)


def quotes_site(**changes) -> dict[str, FakePage]:
    urls = (FIRST_QUOTES_URL, SECOND_QUOTES_URL, THIRD_QUOTES_URL)
    return {url: silent_timer_page(**changes) for url in urls}


def server_rendered_quotes() -> dict:
    urls = (FIRST_QUOTES_URL, SECOND_QUOTES_URL, THIRD_QUOTES_URL)
    return {url: http_page(article_html(url), url) for url in urls}


def quick_page(url: str, **changes) -> FakePage:
    return FakePage(
        html=article_html(url), probes=[stable_probe()], probe_seconds=PROBE_SECONDS, **changes
    )


class Harness:
    """A runtime with fake Chrome on a manual clock and late-content observation enabled."""

    def __init__(self, launcher: FakeLauncher, fetcher: FakeHttpFetcher, runtime: Runtime):
        self.launcher = launcher
        self.fetcher = fetcher
        self.runtime = runtime

    async def scrape(self, url: str, **changes) -> ScrapeResult:
        values = {
            "url": url,
            "mode": "auto",
            "wait_for": None,
            "selector": None,
            "timeout_seconds": LEARNING_TIMEOUT_SECONDS,
            "use_proxy": False,
            "block_resources": frozenset(),
        }
        values.update(changes)
        return await self.runtime.scraper.scrape(ScrapeRequest(**values))

    async def settle(self) -> None:
        """Let late-content observation and the verification it starts finish."""
        await self.runtime.pool.drain()
        await self.runtime.verifier.drain()

    def verdict(self, url: str) -> Verdict:
        return self.runtime.verdicts.get(section_key(url))

    def fetched_urls(self) -> list[str]:
        return [request.url for request in self.fetcher.requests]

    def min_ready(self, url: str) -> float:
        hints = self.runtime.profiles.hints(section_key(url))
        return hints.min_ready_seconds if hints else 0.0


async def start_harness(
    pages: dict[str, FakePage],
    clock: Clock | None = None,
    chance: Callable[[], float] = lambda: NEVER_SAMPLED,
    **env: str,
) -> Harness:
    manual = clock if isinstance(clock, ManualClock) else None
    launcher = FakeLauncher(pages, manual)
    fetcher = FakeHttpFetcher(server_rendered_quotes())
    adapters = Adapters(
        launcher=launcher,
        fetcher_factory=lambda guard, settings: fetcher,
        resolver=public_resolver,
        pause=lambda: 0.0,
        clock=clock or time.monotonic,
        chance=chance,
    )
    overrides = {"MAX_WORKERS": "1", "LATE_CONTENT_OBSERVE_SECONDS": OBSERVE_SECONDS, **env}
    settings = make_settings(**overrides)
    return Harness(launcher, fetcher, await start_runtime(settings, adapters))


async def wait_until(condition: Callable[[], bool]) -> None:
    while not condition():
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_silent_timer_section_is_learned_and_the_next_render_waits_for_its_content():
    clock = ManualClock()
    harness = await start_harness(quotes_site(), clock)

    cold = await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()
    timer = PhaseTimer()
    started = clock.now
    learned = await harness.scrape(SECOND_QUOTES_URL, timer=timer)

    assert cold.profile == "cold"
    assert "class='quote'" not in cold.html
    assert learned.profile == "learned"
    assert "class='quote'" in learned.html
    assert learned.ready_reason == "settled"
    assert clock.now - started >= LATE_CONTENT_SECONDS
    assert timer.notes()["profile"] == "learned"
    expected_ms = round((LATE_CONTENT_SECONDS + MIN_READY_MARGIN_SECONDS) * 1000)
    assert timer.notes()["min_ready_ms"] == str(expected_ms)


@pytest.mark.anyio
async def test_late_content_makes_the_section_browser_only():
    harness = await start_harness(quotes_site(), ManualClock(), VERDICT_MIN_SAMPLES="1")

    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()

    assert harness.runtime.verdicts.get(section_key(FIRST_QUOTES_URL)) is Verdict.BROWSER_REQUIRED


@pytest.mark.anyio
async def test_learned_floor_never_shortens_a_page_that_settles_later():
    clock = ManualClock()
    quick = FakePage(probes=[stable_probe()], probe_seconds=PROBE_SECONDS)
    growing_probes = 40
    slow = FakePage(
        probes=[stable_probe(growth=count) for count in range(growing_probes)],
        probe_seconds=PROBE_SECONDS,
    )
    pages = {FIRST_QUOTES_URL: quick, SECOND_QUOTES_URL: slow}
    harness = await start_harness(pages, clock)
    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()
    learned_min_ready = harness.min_ready(FIRST_QUOTES_URL)
    started = clock.now

    result = await harness.scrape(SECOND_QUOTES_URL)

    assert result.profile == "learned"
    assert learned_min_ready < growing_probes * PROBE_SECONDS
    assert clock.now - started >= growing_probes * PROBE_SECONDS


@pytest.mark.anyio
async def test_sections_are_observed_only_for_the_first_renders_unless_sampled():
    clock = ManualClock()
    quick_pages = {
        url: FakePage(probes=[stable_probe()], probe_seconds=PROBE_SECONDS)
        for url in (FIRST_QUOTES_URL, SECOND_QUOTES_URL)
    }
    pages = {**quick_pages, THIRD_QUOTES_URL: silent_timer_page()}
    # Without the fast path no section waits for a verdict, so only sampling applies.
    harness = await start_harness(
        pages, clock, chance=lambda: NEVER_SAMPLED, HTTP_FIRST_ENABLED="false"
    )
    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()
    await harness.scrape(SECOND_QUOTES_URL)
    await harness.settle()

    await harness.scrape(THIRD_QUOTES_URL)
    await harness.settle()

    assert PROFILE_OBSERVE_FIRST_RENDERS == 2
    assert harness.min_ready(THIRD_QUOTES_URL) < LATE_CONTENT_SECONDS


@pytest.mark.anyio
async def test_observation_is_skipped_when_a_request_is_waiting_for_a_browser():
    clock = ManualClock()
    gate = asyncio.Event()
    pages = {
        FIRST_QUOTES_URL: silent_timer_page(gate=gate, gate_from_probe=1),
        OTHER_SITE_URL: FakePage(probes=[stable_probe()], probe_seconds=PROBE_SECONDS),
    }
    harness = await start_harness(pages, clock)
    pool = harness.runtime.pool
    first = asyncio.create_task(harness.scrape(FIRST_QUOTES_URL))
    await wait_until(lambda: pool.stats().busy == 1)
    queued = asyncio.create_task(harness.scrape(OTHER_SITE_URL))
    await wait_until(lambda: pool.stats().waiting == 1)

    gate.set()
    await asyncio.gather(first, queued)
    await harness.settle()

    assert harness.min_ready(FIRST_QUOTES_URL) < LATE_CONTENT_SECONDS
    assert harness.launcher.launched[0].closed_tabs == 2


@pytest.mark.anyio
async def test_running_observation_gives_its_browser_to_a_new_request(caplog):
    # Real clock and a long observation: if the observation kept its browser, the new
    # request would wait beyond the one-second queue timeout and fail.
    pages = {
        FIRST_QUOTES_URL: silent_timer_page(probes=[stable_probe()]),
        # An error page teaches nothing, so the new request does not start a 30 s watch.
        OTHER_SITE_URL: FakePage(status=404),
    }
    harness = await start_harness(pages, LATE_CONTENT_OBSERVE_SECONDS="30")
    await harness.scrape(FIRST_QUOTES_URL)
    await wait_until(lambda: harness.runtime.pool.stats().observing == 1)

    with caplog.at_level(logging.INFO, logger="render.pool"):
        result = await harness.scrape(OTHER_SITE_URL)
    await harness.settle()

    assert result.engine == "browser"
    assert "cut short" in caplog.text
    assert harness.runtime.pool.stats().observing == 0
    assert harness.launcher.launched[0].closed_tabs == 2


@pytest.mark.anyio
async def test_failed_observation_is_logged_and_does_not_affect_the_returned_page(caplog):
    broken = [stable_probe(), stable_probe(), ConnectionError("websocket closed")]
    harness = await start_harness(quotes_site(probes=broken), ManualClock())

    with caplog.at_level(logging.WARNING, logger="render.browser"):
        result = await harness.scrape(FIRST_QUOTES_URL)
        await harness.settle()

    assert result.html == SERVER_RENDERED_HTML
    assert result.ready_reason == "settled"
    assert "Late-content observation failed" in caplog.text
    assert harness.launcher.launched[0].closed_tabs == 1
    assert harness.min_ready(FIRST_QUOTES_URL) < LATE_CONTENT_SECONDS


@pytest.mark.anyio
async def test_renders_with_blocked_resources_are_not_learned():
    harness = await start_harness(quotes_site(), ManualClock())

    await harness.scrape(FIRST_QUOTES_URL, block_resources=frozenset({BlockedResource.IMAGE}))
    await harness.settle()

    assert harness.runtime.profiles.hints(section_key(FIRST_QUOTES_URL)) is None
    assert harness.runtime.profiles.count() == 0


@pytest.mark.anyio
async def test_unstable_renders_are_not_learned():
    restless = [stable_probe(growth=count) for count in range(100_000)]
    harness = await start_harness(quotes_site(probes=restless), ManualClock())

    result = await harness.scrape(FIRST_QUOTES_URL, timeout_seconds=1.0)
    await harness.settle()

    assert result.stable is False
    assert harness.runtime.profiles.count() == 0


@pytest.mark.anyio
async def test_disabled_observation_still_learns_the_timing_of_each_render():
    harness = await start_harness(quotes_site(), ManualClock(), LATE_CONTENT_OBSERVE_SECONDS="0")

    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()
    result = await harness.scrape(SECOND_QUOTES_URL)

    assert result.profile == "learned"
    assert harness.runtime.pool.stats().observing == 0
    assert "class='quote'" not in result.html


@pytest.mark.anyio
async def test_verification_starts_only_after_the_late_watch_finished():
    harness = await start_harness({FIRST_QUOTES_URL: quick_page(FIRST_QUOTES_URL)}, ManualClock())

    await harness.scrape(FIRST_QUOTES_URL)
    await harness.runtime.verifier.drain()
    fetched_before_watch_ended = harness.fetched_urls()
    await harness.settle()

    assert fetched_before_watch_ended == []
    assert harness.fetched_urls() == [FIRST_QUOTES_URL]


@pytest.mark.anyio
async def test_disabled_late_watch_never_verifies_a_section():
    pages = {url: quick_page(url) for url in (FIRST_QUOTES_URL, SECOND_QUOTES_URL)}
    harness = await start_harness(pages, ManualClock(), LATE_CONTENT_OBSERVE_SECONDS="0")

    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()
    await harness.scrape(SECOND_QUOTES_URL)
    await harness.settle()

    assert harness.fetched_urls() == []
    assert harness.verdict(FIRST_QUOTES_URL) is Verdict.UNKNOWN


@pytest.mark.anyio
async def test_render_with_late_content_is_not_used_as_verification_reference():
    harness = await start_harness(quotes_site(), ManualClock())

    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()

    assert harness.fetched_urls() == []
    assert harness.verdict(FIRST_QUOTES_URL) is Verdict.BROWSER_REQUIRED


@pytest.mark.anyio
async def test_failed_late_watch_does_not_verify():
    broken = [stable_probe(), stable_probe(), ConnectionError("websocket closed")]
    harness = await start_harness(quotes_site(probes=broken), ManualClock())

    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()

    assert harness.fetched_urls() == []


@pytest.mark.anyio
async def test_late_watch_that_never_reads_the_page_teaches_nothing(caplog):
    swap = ProtocolException({"message": "Execution context was destroyed", "code": -32000})
    page = FakePage(html=article_html(FIRST_QUOTES_URL), probes=[stable_probe()] * 2 + [swap])
    pages = {FIRST_QUOTES_URL: page}
    harness = await start_harness(pages, ManualClock())

    with caplog.at_level(logging.INFO, logger="render.browser"):
        await harness.scrape(FIRST_QUOTES_URL)
        await harness.settle()

    assert harness.fetched_urls() == []
    assert harness.verdict(FIRST_QUOTES_URL) is Verdict.UNKNOWN
    await wait_until(lambda: harness.runtime.pool.stats().recycling == 0)
    assert "could not read the page" in caplog.text
    assert len(harness.launcher.launched) == 1


@pytest.mark.anyio
async def test_cut_short_late_watch_does_not_verify():
    pages = {
        FIRST_QUOTES_URL: silent_timer_page(probes=[stable_probe()]),
        OTHER_SITE_URL: FakePage(status=404),
    }
    harness = await start_harness(pages, LATE_CONTENT_OBSERVE_SECONDS="30")
    await harness.scrape(FIRST_QUOTES_URL)
    await wait_until(lambda: harness.runtime.pool.stats().observing == 1)

    await harness.scrape(OTHER_SITE_URL)
    await harness.settle()

    assert harness.fetched_urls() == []


@pytest.mark.anyio
async def test_skipped_late_watch_does_not_verify():
    clock = ManualClock()
    gate = asyncio.Event()
    pages = {
        FIRST_QUOTES_URL: quick_page(FIRST_QUOTES_URL, gate=gate, gate_from_probe=1),
        OTHER_SITE_URL: FakePage(status=404),
    }
    harness = await start_harness(pages, clock)
    pool = harness.runtime.pool
    first = asyncio.create_task(harness.scrape(FIRST_QUOTES_URL))
    await wait_until(lambda: pool.stats().busy == 1)
    queued = asyncio.create_task(harness.scrape(OTHER_SITE_URL))
    await wait_until(lambda: pool.stats().waiting == 1)

    gate.set()
    await asyncio.gather(first, queued)
    await harness.settle()

    assert harness.fetched_urls() == []


@pytest.mark.anyio
async def test_unverified_section_is_watched_and_verified_beyond_its_first_renders():
    pages = {url: quick_page(url) for url in server_rendered_quotes()}
    harness = await start_harness(
        pages, ManualClock(), chance=lambda: NEVER_SAMPLED, VERDICT_MIN_SAMPLES="3"
    )

    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()
    await harness.scrape(SECOND_QUOTES_URL)
    await harness.settle()
    await harness.scrape(THIRD_QUOTES_URL)
    await harness.settle()

    assert harness.verdict(THIRD_QUOTES_URL) is Verdict.HTTP_SUFFICIENT


@pytest.mark.anyio
async def test_single_page_section_uses_http_after_matching_again_after_the_interval():
    clock = ManualClock()
    harness = await start_harness({FIRST_QUOTES_URL: quick_page(FIRST_QUOTES_URL)}, clock)
    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()
    clock.advance(SAME_PAGE_INTERVAL_SECONDS)
    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()

    result = await harness.scrape(FIRST_QUOTES_URL)

    assert harness.verdict(FIRST_QUOTES_URL) is Verdict.HTTP_SUFFICIENT
    assert result.engine == "http"


@pytest.mark.anyio
async def test_single_page_is_not_fetched_again_within_the_interval():
    harness = await start_harness({FIRST_QUOTES_URL: quick_page(FIRST_QUOTES_URL)}, ManualClock())
    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()

    await harness.scrape(FIRST_QUOTES_URL)
    await harness.settle()

    assert harness.fetched_urls() == [FIRST_QUOTES_URL]
    assert harness.verdict(FIRST_QUOTES_URL) is Verdict.UNKNOWN
