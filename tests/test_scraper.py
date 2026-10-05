import time

import pytest
from zendriver import cdp

import app.browser.page_loader as page_loader
import app.browser.readiness as readiness
from app.browser.blocking import BlockedResource
from app.errors import (
    NavigationError,
    ProxyNotConfiguredError,
    TargetBlockedError,
    TargetNotAllowedError,
)
from app.runtime import Adapters, Runtime, start_runtime
from app.scraping.scraper import (
    FAST_PATH_MAX_SECONDS,
    FAST_PATH_TIMEOUT_SHARE,
    PROFILE_NOT_APPLICABLE,
    VERIFIED_HTTP_READY_REASON,
    ScrapeRequest,
)
from app.scraping.verdicts import Verdict, section_key
from app.timing import PhaseTimer
from tests.fakes import (
    CHALLENGE_HTML,
    SERVER_RENDERED_HTML,
    SPA_SHELL_HTML,
    FakeHttpFetcher,
    FakeLauncher,
    FakePage,
    ManualClock,
    article_html,
    http_page,
    make_settings,
    public_resolver,
    request_sent,
    restless_probes,
    stable_probe,
)

ARTICLE_URL = "https://news.example/articles/1"
OTHER_ARTICLE_URL = "https://news.example/articles/2"
APP_URL = "https://app.example/dashboard"
SHORT_BUDGET_SECONDS = 0.05
TRACKER_URL = "https://www.google-analytics.com/g/collect"
# A render is a verification reference only after a clean late-content watch; a brief
# one keeps these real-clock tests fast.
BRIEF_WATCH_SECONDS = "0.01"


def scrape_request(url: str, **changes) -> ScrapeRequest:
    values = {
        "url": url,
        "mode": "auto",
        "wait_for": None,
        "selector": None,
        "timeout_seconds": 5.0,
        "use_proxy": False,
        "block_resources": frozenset(),
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
        # Verification starts when the late-content watch of the render ends.
        await self.runtime.pool.drain()
        await self.runtime.verifier.drain()
        return result

    def verdict(self, url: str) -> Verdict:
        return self.runtime.verdicts.get(section_key(url))


async def start_harness(
    responses: dict, pages: dict | None = None, clock: ManualClock | None = None, **env: str
) -> Harness:
    """A harness on the real clock, or on ``clock``, which then times the fakes too."""
    fetcher = FakeHttpFetcher(responses, clock=clock)
    launcher = FakeLauncher(server_rendered_pages() if pages is None else pages, clock)
    adapters = Adapters(
        launcher=launcher,
        fetcher_factory=lambda guard, settings: fetcher,
        resolver=public_resolver,
        pause=lambda: 0.0,
        clock=clock or time.monotonic,
    )
    overrides = {"MAX_WORKERS": "1", "LATE_CONTENT_OBSERVE_SECONDS": BRIEF_WATCH_SECONDS, **env}
    runtime = await start_runtime(make_settings(**overrides), adapters)
    return Harness(fetcher, launcher, runtime)


def server_rendered_site() -> dict:
    return {url: http_page(article_html(url), url) for url in (ARTICLE_URL, OTHER_ARTICLE_URL)}


def server_rendered_pages() -> dict:
    return {url: FakePage(html=article_html(url)) for url in (ARTICLE_URL, OTHER_ARTICLE_URL)}


@pytest.mark.anyio
async def test_unknown_section_is_rendered_in_browser():
    harness = await start_harness(server_rendered_site())

    result = await harness.scrape(ARTICLE_URL)

    assert result.engine == "browser"
    assert result.html == article_html(ARTICLE_URL)


@pytest.mark.anyio
async def test_section_switches_to_http_after_enough_matching_samples():
    harness = await start_harness(server_rendered_site())

    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)
    result = await harness.scrape(ARTICLE_URL)

    assert harness.verdict(ARTICLE_URL) is Verdict.HTTP_SUFFICIENT
    assert result.engine == "http"


@pytest.mark.anyio
async def test_browser_render_reports_why_it_was_considered_ready():
    harness = await start_harness(server_rendered_site())

    result = await harness.scrape(ARTICLE_URL)

    assert result.ready_reason == "settled"


@pytest.mark.anyio
async def test_verified_http_result_reports_verified_http_as_ready_reason():
    harness = await start_harness(server_rendered_site())
    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)

    result = await harness.scrape(ARTICLE_URL)

    assert result.engine == "http"
    assert result.ready_reason == VERIFIED_HTTP_READY_REASON
    assert result.profile == PROFILE_NOT_APPLICABLE


@pytest.mark.anyio
async def test_browser_render_observes_in_an_isolated_world_without_enabling_runtime():
    harness = await start_harness({})

    await harness.scrape(ARTICLE_URL, mode="browser")

    sent = harness.launcher.launched[0].sent_methods
    assert "Page.createIsolatedWorld" in sent
    assert "Runtime.enable" not in sent


@pytest.mark.anyio
async def test_network_tracker_listens_only_while_the_page_renders():
    harness = await start_harness({})

    await harness.scrape(ARTICLE_URL, mode="browser")

    [tab] = harness.launcher.launched[0].tabs
    assert cdp.network.RequestWillBeSent in tab.handlers_seen
    assert all(not handlers for handlers in tab.handlers.values())


@pytest.mark.anyio
async def test_tracker_requests_are_logged_as_ignored_and_do_not_hold_the_render_back():
    beacons = [request_sent(str(index), TRACKER_URL, "Script") for index in range(3)]
    page = FakePage(events={0: beacons})
    harness = await start_harness({}, pages={ARTICLE_URL: page})
    timer = PhaseTimer()

    result = await harness.scrape(ARTICLE_URL, mode="browser", timer=timer)

    assert result.ready_reason == "settled"
    assert timer.notes()["inflight_ignored"] == str(len(beacons))


@pytest.mark.anyio
async def test_open_content_request_holds_the_render_back_until_the_load_budget(monkeypatch):
    monkeypatch.setattr(page_loader, "MIN_READINESS_BUDGET_SECONDS", SHORT_BUDGET_SECONDS)
    page = FakePage(events={0: [request_sent("1", f"{ARTICLE_URL}/comments", "Fetch")]})
    harness = await start_harness({}, pages={ARTICLE_URL: page})

    result = await harness.scrape(ARTICLE_URL, mode="browser", timeout_seconds=SHORT_BUDGET_SECONDS)

    assert result.ready_reason == "load-budget-expired"


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


async def verified_harness(**changes) -> Harness:
    harness = await start_harness(server_rendered_site(), **changes)
    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)
    return harness


@pytest.mark.anyio
async def test_http_redirect_to_a_forbidden_target_falls_back_to_browser_without_mismatch():
    harness = await verified_harness()
    harness.fetcher.failures[ARTICLE_URL] = TargetNotAllowedError("redirect to a private address")

    result = await harness.scrape(ARTICLE_URL)

    assert result.engine == "browser"
    assert harness.verdict(ARTICLE_URL) is Verdict.HTTP_SUFFICIENT


@pytest.mark.parametrize(
    ("timeout_seconds", "fast_path_seconds"),
    [(10.0, 10.0 * FAST_PATH_TIMEOUT_SHARE), (60.0, FAST_PATH_MAX_SECONDS)],
)
@pytest.mark.anyio
async def test_fast_path_gets_a_capped_share_of_the_timeout(timeout_seconds, fast_path_seconds):
    harness = await verified_harness()

    await harness.scrape(ARTICLE_URL, timeout_seconds=timeout_seconds)

    assert harness.fetcher.requests[-1].timeout_seconds == pytest.approx(fast_path_seconds)


@pytest.mark.anyio
async def test_browser_after_a_failed_fast_path_gets_only_the_remaining_budget():
    timeout_seconds = 10.0
    clock = ManualClock()
    harness = await verified_harness(clock=clock)
    harness.launcher.pages[ARTICLE_URL] = FakePage(probes=restless_probes())
    harness.fetcher.failures[ARTICLE_URL] = NavigationError("HTTP fetch timed out")
    harness.fetcher.fetch_seconds = timeout_seconds * FAST_PATH_TIMEOUT_SHARE
    started = clock.now

    result = await harness.scrape(ARTICLE_URL, timeout_seconds=timeout_seconds)

    assert result.ready_reason == "deadline"
    assert clock.now - started == pytest.approx(timeout_seconds, abs=FakePage().probe_seconds)


@pytest.mark.anyio
async def test_error_status_in_browser_is_not_used_as_verification_reference():
    harness = await start_harness(server_rendered_site(), pages={ARTICLE_URL: FakePage(status=404)})

    result = await harness.scrape(ARTICLE_URL)

    assert result.upstream_status == 404
    assert harness.fetcher.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kinds",
    [
        frozenset({BlockedResource.IMAGE}),
        frozenset({BlockedResource.FONT, BlockedResource.STYLESHEET}),
    ],
)
async def test_render_with_blocked_resources_is_not_used_as_verification_reference(kinds):
    harness = await start_harness(server_rendered_site())

    result = await harness.scrape(ARTICLE_URL, block_resources=kinds)

    assert result.engine == "browser"
    assert result.stable
    assert harness.fetcher.requests == []
    assert harness.verdict(ARTICLE_URL) is Verdict.UNKNOWN


@pytest.mark.anyio
async def test_render_without_blocked_resources_is_used_as_verification_reference():
    harness = await start_harness(server_rendered_site())

    await harness.scrape(ARTICLE_URL, block_resources=frozenset())

    assert [request.url for request in harness.fetcher.requests] == [ARTICLE_URL]


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
        "blocked": "none",
        "route": "direct",
        "escalated": "false",
        "profile": "cold",
        "min_ready_ms": "0",
        "readiness_end": "settled",
        "challenge_polls": "0",
        "quiet_ms": "0",
        "inflight_ignored": "0",
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
    assert timer.notes() == {"blocked": "none", "route": "direct", "escalated": "false"}


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
    return FakePage(probes=restless_probes())


@pytest.mark.anyio
async def test_restless_page_is_not_cut_short_before_the_wait_for_quiet_cap(monkeypatch):
    monkeypatch.setattr(page_loader, "MIN_READINESS_BUDGET_SECONDS", SHORT_BUDGET_SECONDS)
    harness = await start_harness({}, pages={ARTICLE_URL: restless_page()})
    timer = PhaseTimer()

    result = await harness.scrape(
        ARTICLE_URL, wait_for="#price", timeout_seconds=SHORT_BUDGET_SECONDS, timer=timer
    )

    assert result.stable is False
    assert timer.notes()["readiness_end"] == "deadline"


@pytest.mark.anyio
async def test_restless_page_is_returned_once_the_element_outlasted_the_cap(monkeypatch):
    monkeypatch.setattr(page_loader, "MIN_READINESS_BUDGET_SECONDS", SHORT_BUDGET_SECONDS)
    monkeypatch.setattr(readiness, "WAIT_FOR_QUIET_CAP_SECONDS", 0)
    harness = await start_harness({}, pages={ARTICLE_URL: restless_page()})
    timer = PhaseTimer()

    result = await harness.scrape(
        ARTICLE_URL, wait_for="#price", timeout_seconds=SHORT_BUDGET_SECONDS, timer=timer
    )

    assert result.stable is False
    assert timer.notes()["readiness_end"] == "wait-for-found"
