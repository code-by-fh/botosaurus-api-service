"""Automatic escalation to HOME_PROXY when the direct route is blocked by bot protection."""

import pytest

from app.errors import TargetBlockedError
from app.timing import PhaseTimer
from tests.fakes import (
    CHALLENGE_HTML,
    FakePage,
    ManualClock,
    article_html,
    restless_probes,
    stable_probe,
)
from tests.test_scraper import (
    ARTICLE_URL,
    OTHER_ARTICLE_URL,
    Harness,
    server_rendered_site,
    start_harness,
)

HOME_PROXY = "http://home.example:8888"
HOST = "news.example"
PATIENCE_SECONDS = 2.0
TIMEOUT_SECONDS = 20.0
TTL_SECONDS = 60.0
# Every observation of a fake tab moves the manual clock by this much.
PROBE_SECONDS = FakePage().probe_seconds
# Shorter than the patience plus the smallest readiness budget, so no retry can follow.
TOO_SHORT_TIMEOUT_SECONDS = 2.5


def challenged(via_proxy: FakePage | None) -> FakePage:
    """A page that keeps showing a challenge on the direct route."""
    return FakePage(html=CHALLENGE_HTML, probes=[stable_probe(challenge=True)], via_proxy=via_proxy)


def blocked_on_direct_route() -> dict:
    """Both articles are challenged on the direct route and served through HOME_PROXY."""
    return {
        url: challenged(via_proxy=FakePage(html=article_html(url)))
        for url in (ARTICLE_URL, OTHER_ARTICLE_URL)
    }


async def escalation_harness(pages: dict, clock: ManualClock | None = None, **env: str) -> Harness:
    settings = {
        "HOME_PROXY": HOME_PROXY,
        "AUTO_PROXY_CHALLENGE_SECONDS": str(PATIENCE_SECONDS),
        "AUTO_PROXY_TTL_SECONDS": str(TTL_SECONDS),
        **env,
    }
    return await start_harness({}, pages=pages, clock=clock or ManualClock(), **settings)


def routes(harness: Harness) -> list[str]:
    """The egress route of every browser context opened so far, in order."""
    direct = harness.runtime.egress.url_for(use_proxy=False)
    proxies = harness.launcher.launched[0].context_proxies
    return ["direct" if proxy == direct else "proxy" for proxy in proxies]


@pytest.mark.anyio
async def test_persistent_challenge_on_direct_route_is_retried_through_the_proxy():
    clock = ManualClock()
    harness = await escalation_harness(blocked_on_direct_route(), clock)
    timer = PhaseTimer()

    result = await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS, timer=timer)

    assert result.html == article_html(ARTICLE_URL)
    assert result.route == "proxy"
    assert routes(harness) == ["direct", "proxy"]
    assert timer.notes()["route"] == "proxy"
    assert timer.notes()["escalated"] == "true"


@pytest.mark.anyio
async def test_direct_attempt_gives_up_after_the_challenge_patience_not_the_timeout():
    clock = ManualClock()
    pages = {ARTICLE_URL: challenged(via_proxy=FakePage(html=article_html(ARTICLE_URL)))}
    harness = await escalation_harness(pages, clock)
    started = clock.now

    await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS)

    # Direct: one poll to see the challenge plus the patience; proxy: two polls to settle.
    assert clock.now - started == pytest.approx(PATIENCE_SECONDS + 3 * PROBE_SECONDS)


@pytest.mark.anyio
async def test_proxy_attempt_gets_only_the_remaining_budget():
    clock = ManualClock()
    pages = {ARTICLE_URL: challenged(via_proxy=FakePage(probes=restless_probes()))}
    harness = await escalation_harness(pages, clock)
    started = clock.now

    result = await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS)

    assert result.ready_reason == "deadline"
    assert clock.now - started == pytest.approx(TIMEOUT_SECONDS, abs=2 * PROBE_SECONDS)


@pytest.mark.anyio
async def test_escalated_render_is_not_used_as_verification_reference():
    harness = await escalation_harness(blocked_on_direct_route())

    await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS)

    assert harness.fetcher.requests == []


@pytest.mark.anyio
async def test_host_that_needed_the_proxy_goes_through_it_from_the_start():
    harness = await escalation_harness(blocked_on_direct_route())
    await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS)
    timer = PhaseTimer()

    result = await harness.scrape(OTHER_ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS, timer=timer)

    assert result.html == article_html(OTHER_ARTICLE_URL)
    assert result.route == "proxy"
    assert routes(harness) == ["direct", "proxy", "proxy"]
    assert timer.notes()["escalated"] == "false"
    assert harness.runtime.proxy_hosts.count() == 1


@pytest.mark.anyio
async def test_remembered_host_skips_the_http_fast_path_and_is_not_verified():
    harness = await start_harness(server_rendered_site(), HOME_PROXY=HOME_PROXY)
    await harness.scrape(ARTICLE_URL)
    await harness.scrape(OTHER_ARTICLE_URL)
    fetches = len(harness.fetcher.requests)
    harness.runtime.proxy_hosts.remember(HOST)

    result = await harness.scrape(ARTICLE_URL)

    assert result.engine == "browser"
    assert result.route == "proxy"
    assert len(harness.fetcher.requests) == fetches


@pytest.mark.anyio
async def test_remembered_host_is_tried_directly_again_after_the_ttl():
    clock = ManualClock()
    harness = await escalation_harness(blocked_on_direct_route(), clock)
    await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS)
    clock.advance(TTL_SECONDS)

    await harness.scrape(OTHER_ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS)

    assert routes(harness) == ["direct", "proxy", "direct", "proxy"]


@pytest.mark.anyio
async def test_challenge_that_clears_within_the_patience_is_not_escalated():
    clearing = [stable_probe(challenge=True)] * 4 + [stable_probe(growth=1)]
    page = FakePage(probes=clearing, via_proxy=FakePage(html=CHALLENGE_HTML))
    harness = await escalation_harness({ARTICLE_URL: page})
    timer = PhaseTimer()

    result = await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS, timer=timer)

    assert result.route == "direct"
    assert routes(harness) == ["direct"]
    assert timer.notes()["escalated"] == "false"
    assert harness.runtime.proxy_hosts.count() == 0


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"HOME_PROXY": ""}, id="no-home-proxy"),
        pytest.param({"AUTO_PROXY_ON_BLOCK": "false"}, id="disabled"),
    ],
)
@pytest.mark.anyio
async def test_no_escalation_without_proxy_or_when_disabled(env):
    clock = ManualClock()
    harness = await escalation_harness(blocked_on_direct_route(), clock, **env)
    started = clock.now

    with pytest.raises(TargetBlockedError):
        await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS)

    assert routes(harness) == ["direct"]
    # Without a retry to follow, the challenge is given the whole budget, as before.
    assert clock.now - started == pytest.approx(TIMEOUT_SECONDS, abs=2 * PROBE_SECONDS)


@pytest.mark.anyio
async def test_request_that_chose_the_proxy_is_not_escalated_again():
    pages = {ARTICLE_URL: challenged(via_proxy=challenged(via_proxy=None))}
    harness = await escalation_harness(pages)

    with pytest.raises(TargetBlockedError):
        await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS, use_proxy=True)

    assert routes(harness) == ["proxy"]


@pytest.mark.anyio
async def test_blocked_on_both_routes_is_target_blocked_and_not_remembered():
    pages = {ARTICLE_URL: challenged(via_proxy=challenged(via_proxy=None))}
    harness = await escalation_harness(pages)
    timer = PhaseTimer()

    with pytest.raises(TargetBlockedError):
        await harness.scrape(ARTICLE_URL, timeout_seconds=TIMEOUT_SECONDS, timer=timer)

    assert routes(harness) == ["direct", "proxy"]
    assert timer.notes()["escalated"] == "true"
    assert harness.runtime.proxy_hosts.count() == 0


@pytest.mark.anyio
async def test_too_little_budget_left_is_target_blocked_without_retry():
    harness = await escalation_harness(blocked_on_direct_route())
    timer = PhaseTimer()

    with pytest.raises(TargetBlockedError):
        await harness.scrape(ARTICLE_URL, timeout_seconds=TOO_SHORT_TIMEOUT_SECONDS, timer=timer)

    assert routes(harness) == ["direct"]
    assert timer.notes()["escalated"] == "false"
