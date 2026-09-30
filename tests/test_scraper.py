import pytest

import app.browser.page_loader as page_loader
from app.errors import ProxyNotConfiguredError, TargetBlockedError, TargetNotAllowedError
from app.runtime import Adapters, Runtime, start_runtime
from app.scraping.scraper import ScrapeRequest
from app.scraping.verdicts import Verdict, section_key
from app.timing import PhaseTimer
from tests.fakes import (
    CHALLENGE_HTML,
    SERVER_RENDERED_HTML,
    SPA_SHELL_HTML,
    FakeHttpFetcher,
    FakeLauncher,
    FakePage,
    http_page,
    make_settings,
    public_resolver,
    stable_probe,
)

ARTICLE_URL = "https://news.example/articles/1"
OTHER_ARTICLE_URL = "https://news.example/articles/2"
APP_URL = "https://app.example/dashboard"
SHORT_BUDGET_SECONDS = 0.05


def scrape_request(url: str, **changes) -> ScrapeRequest:
    values = {
        "url": url,
        "mode": "auto",
        "wait_for": None,
        "selector": None,
        "timeout_seconds": 5.0,
        "use_proxy": False,
        "block_resources": False,
    }
    values.update(changes)
    return ScrapeRequest(**values)


class Harness:
    """A runtime wired to fake Chrome and fake HTTP."""

    def __init__(self, fetcher: FakeHttpFetcher, launcher: FakeLauncher, runtime: Runtime):
        self.fetcher = fetcher
        self.launcher = launcher
        self.runtime = runtime

    async def scrape(self, url: str, **changes):
        result = await self.runtime.scraper.scrape(scrape_request(url, **changes))
        await self.runtime.verifier.drain()
        return result

    def verdict(self, url: str) -> Verdict:
        return self.runtime.verdicts.get(section_key(url))


async def start_harness(responses: dict, pages: dict | None = None, **env: str) -> Harness:
    fetcher = FakeHttpFetcher(responses)
    launcher = FakeLauncher(pages or {})
    adapters = Adapters(
        launcher=launcher,
        fetcher_factory=lambda guard, settings: fetcher,
        resolver=public_resolver,
        pause=lambda: 0.0,
    )
    runtime = await start_runtime(make_settings(MAX_WORKERS="1", **env), adapters)
    return Harness(fetcher, launcher, runtime)


def server_rendered_site() -> dict:
    return {
        ARTICLE_URL: http_page(SERVER_RENDERED_HTML, ARTICLE_URL),
        OTHER_ARTICLE_URL: http_page(SERVER_RENDERED_HTML, OTHER_ARTICLE_URL),
    }


@pytest.mark.anyio
async def test_unknown_section_is_rendered_in_browser():
    harness = await start_harness(server_rendered_site())

    result = await harness.scrape(ARTICLE_URL)

    assert result.engine == "browser"
    assert result.html == SERVER_RENDERED_HTML


@pytest.mark.anyio
async def test_section_switches_to_http_after_enough_matching_samples():
    harness = await start_harness(server_rendered_site())

    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)
    result = await harness.scrape(ARTICLE_URL)

    assert harness.verdict(ARTICLE_URL) is Verdict.HTTP_SUFFICIENT
    assert result.engine == "http"


@pytest.mark.anyio
async def test_javascript_only_section_stays_in_browser():
    harness = await start_harness({APP_URL: http_page(SPA_SHELL_HTML, APP_URL)})

    await harness.scrape(APP_URL)
    result = await harness.scrape(APP_URL)

    assert harness.verdict(APP_URL) is Verdict.BROWSER_REQUIRED
    assert result.engine == "browser"


@pytest.mark.anyio
async def test_http_missing_text_of_browser_render_is_a_mismatch():
    partial = SERVER_RENDERED_HTML.replace("sentence1 ", "")
    rich_browser_dom = SERVER_RENDERED_HTML.replace(
        "</main>", "<p>price 1299 reviews loaded later</p></main>"
    )
    harness = await start_harness(
        {ARTICLE_URL: http_page(partial, ARTICLE_URL)},
        pages={ARTICLE_URL: FakePage(html=rich_browser_dom)},
        MIN_TEXT_COVERAGE="1.0",
    )

    await harness.scrape(ARTICLE_URL)

    assert harness.verdict(ARTICLE_URL) is Verdict.BROWSER_REQUIRED


@pytest.mark.anyio
async def test_verified_section_falls_back_to_browser_when_http_is_challenged():
    responses = server_rendered_site()
    harness = await start_harness(responses)
    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)
    responses[ARTICLE_URL] = http_page(CHALLENGE_HTML, ARTICLE_URL, status=403)

    result = await harness.scrape(ARTICLE_URL)

    assert result.engine == "browser"
    assert harness.verdict(ARTICLE_URL) is Verdict.BROWSER_REQUIRED


@pytest.mark.anyio
async def test_http_result_without_requested_element_falls_back_to_browser():
    harness = await start_harness(server_rendered_site())
    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)

    result = await harness.scrape(ARTICLE_URL, wait_for="#comments")

    assert result.engine == "browser"


@pytest.mark.anyio
async def test_http_redirect_out_of_verified_section_falls_back_to_browser():
    responses = server_rendered_site()
    harness = await start_harness(responses)
    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)
    responses[ARTICLE_URL] = http_page(SERVER_RENDERED_HTML, "https://news.example/login")

    result = await harness.scrape(ARTICLE_URL)

    assert result.engine == "browser"


@pytest.mark.anyio
async def test_error_status_in_browser_is_not_used_as_verification_reference():
    harness = await start_harness(server_rendered_site(), pages={ARTICLE_URL: FakePage(status=404)})

    result = await harness.scrape(ARTICLE_URL)

    assert result.upstream_status == 404
    assert harness.fetcher.requests == []


@pytest.mark.anyio
async def test_browser_mode_never_uses_http():
    harness = await start_harness(server_rendered_site())

    await harness.scrape(ARTICLE_URL, mode="browser")

    assert harness.fetcher.requests == []


@pytest.mark.anyio
async def test_http_first_can_be_disabled():
    harness = await start_harness(server_rendered_site(), HTTP_FIRST_ENABLED="false")

    await harness.scrape(ARTICLE_URL)

    assert harness.fetcher.requests == []


@pytest.mark.anyio
async def test_failed_verification_fetch_marks_section_browser_only():
    harness = await start_harness({})

    await harness.scrape(ARTICLE_URL)

    assert harness.verdict(ARTICLE_URL) is Verdict.BROWSER_REQUIRED


@pytest.mark.anyio
async def test_use_proxy_without_proxy_is_rejected():
    harness = await start_harness({})

    with pytest.raises(ProxyNotConfiguredError):
        await harness.scrape(ARTICLE_URL, use_proxy=True)


@pytest.mark.anyio
async def test_use_proxy_routes_verification_fetch_through_proxy():
    proxy = "http://home.example:8888"
    harness = await start_harness(server_rendered_site(), HOME_PROXY=proxy)

    await harness.scrape(ARTICLE_URL, use_proxy=True)

    proxied_egress = harness.runtime.egress.url_for(use_proxy=True)
    assert harness.fetcher.requests[0].proxy_url == proxied_egress
    assert harness.launcher.launched[0].context_proxies == [proxied_egress]


@pytest.mark.anyio
async def test_private_targets_are_rejected_before_any_request():
    async def private_resolver(host: str) -> list[str]:
        return ["10.0.0.7"]

    fetcher = FakeHttpFetcher({})
    adapters = Adapters(
        launcher=FakeLauncher(),
        fetcher_factory=lambda guard, settings: fetcher,
        resolver=private_resolver,
    )
    runtime = await start_runtime(make_settings(MAX_WORKERS="1"), adapters)

    with pytest.raises(TargetNotAllowedError):
        await runtime.scraper.scrape(scrape_request("http://intranet.example/"))
    assert fetcher.requests == []


class SteppingClock:
    """Advances one second per reading, so every timed phase lasts a whole number of seconds."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@pytest.mark.anyio
async def test_browser_render_records_each_phase_in_order():
    harness = await start_harness(server_rendered_site())
    timer = PhaseTimer(SteppingClock())

    await harness.scrape(ARTICLE_URL, timer=timer)

    assert list(timer.durations_ms()) == [
        "host_wait",
        "queue",
        "context",
        "navigate",
        "readiness",
        "read",
        "total",
    ]
    assert timer.durations_ms()["queue"] == 1000
    assert timer.notes() == {
        "readiness_end": "settled",
        "challenge_polls": "0",
        "clearance": "none",
    }


@pytest.mark.anyio
async def test_verified_http_result_records_only_the_http_phase():
    harness = await start_harness(server_rendered_site())
    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)
    timer = PhaseTimer(SteppingClock())

    result = await harness.scrape(ARTICLE_URL, timer=timer)

    assert result.engine == "http"
    assert list(timer.durations_ms()) == ["host_wait", "http", "total"]
    assert timer.notes() == {}


@pytest.mark.anyio
async def test_unresolved_challenge_records_readiness_end_and_challenge_polls(monkeypatch):
    monkeypatch.setattr(page_loader, "MIN_READINESS_BUDGET_SECONDS", SHORT_BUDGET_SECONDS)
    challenged = FakePage(html=CHALLENGE_HTML, probes=[stable_probe(challenge=True)])
    harness = await start_harness({}, pages={ARTICLE_URL: challenged})
    timer = PhaseTimer()

    with pytest.raises(TargetBlockedError):
        await harness.scrape(ARTICLE_URL, timer=timer, timeout_seconds=SHORT_BUDGET_SECONDS)

    assert timer.notes()["readiness_end"] == "challenge"
    assert int(timer.notes()["challenge_polls"]) > 0
    assert "read" not in timer.durations_ms()


def restless_page() -> FakePage:
    return FakePage(probes=[stable_probe(text_length=length) for length in range(1, 100_000)])


@pytest.mark.anyio
async def test_restless_page_is_not_cut_short_before_the_default_settle_window(monkeypatch):
    monkeypatch.setattr(page_loader, "MIN_READINESS_BUDGET_SECONDS", SHORT_BUDGET_SECONDS)
    harness = await start_harness({}, pages={ARTICLE_URL: restless_page()})
    timer = PhaseTimer()

    result = await harness.scrape(
        ARTICLE_URL, wait_for="#price", timeout_seconds=SHORT_BUDGET_SECONDS, timer=timer
    )

    assert result.stable is False
    assert timer.notes()["readiness_end"] == "deadline"


@pytest.mark.anyio
async def test_zero_wait_for_settle_returns_restless_page_once_element_is_found(monkeypatch):
    monkeypatch.setattr(page_loader, "MIN_READINESS_BUDGET_SECONDS", SHORT_BUDGET_SECONDS)
    harness = await start_harness({}, pages={ARTICLE_URL: restless_page()})
    timer = PhaseTimer()

    result = await harness.scrape(
        ARTICLE_URL,
        wait_for="#price",
        timeout_seconds=SHORT_BUDGET_SECONDS,
        wait_for_settle_seconds=0.0,
        timer=timer,
    )

    assert result.stable is False
    assert timer.notes()["readiness_end"] == "wait-for-found"
