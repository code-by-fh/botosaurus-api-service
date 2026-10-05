import logging

import pytest

import app.browser.page_loader as page_loader
from app.errors import TargetBlockedError
from app.runtime import Adapters, Runtime, start_runtime
from app.scraping.scraper import ScrapeRequest
from app.timing import PhaseTimer
from tests.fakes import (
    CHALLENGE_HTML,
    FakeHttpFetcher,
    FakeLauncher,
    FakePage,
    cdp_cookie,
    make_settings,
    public_resolver,
    stable_probe,
)

NOW = 1_000_000.0
SHOP_URL = "https://shop.example.com/products/1"
OTHER_SHOP_URL = "https://shop.example.com/products/2"
LOOKALIKE_URL = "https://evil-example.com/products/1"
TOKEN_VALUE = "secret-token-value"
SHORT_BUDGET_SECONDS = 0.05


def waf_token() -> dict:
    return cdp_cookie("aws-waf-token", ".example.com", expires=NOW + 3600, value=TOKEN_VALUE)


def challenged_page() -> FakePage:
    return FakePage(html=CHALLENGE_HTML, probes=[stable_probe(challenge=True)])


class ClearanceHarness:
    """A runtime wired to fake Chrome with a fixed wall clock."""

    def __init__(self, launcher: FakeLauncher, runtime: Runtime):
        self.launcher = launcher
        self.runtime = runtime

    @property
    def browser(self):
        return self.launcher.launched[-1]

    async def render(self, url: str, timer: PhaseTimer | None = None, **changes) -> PhaseTimer:
        timer = timer or PhaseTimer()
        request = ScrapeRequest(
            url=url,
            mode="browser",
            wait_for=None,
            selector=None,
            timeout_seconds=changes.pop("timeout_seconds", 5.0),
            use_proxy=changes.pop("use_proxy", False),
            block_resources=frozenset(),
            timer=timer,
        )
        await self.runtime.scraper.scrape(request)
        return timer

    def injected_names(self) -> list[list[str]]:
        return [[cookie["name"] for cookie in batch] for batch in self.browser.injected]


async def start_harness(pages: dict, **env: str) -> ClearanceHarness:
    launcher = FakeLauncher(pages)
    adapters = Adapters(
        launcher=launcher,
        fetcher_factory=lambda guard, settings: FakeHttpFetcher({}),
        resolver=public_resolver,
        pause=lambda: 0.0,
        wall_clock=lambda: NOW,
    )
    settings = make_settings(MAX_WORKERS="1", HOME_PROXY="http://home.example:8888", **env)
    return ClearanceHarness(launcher, await start_runtime(settings, adapters))


@pytest.fixture
def short_readiness_budget(monkeypatch):
    monkeypatch.setattr(page_loader, "MIN_READINESS_BUDGET_SECONDS", SHORT_BUDGET_SECONDS)


@pytest.mark.anyio
async def test_first_render_stores_the_clearance_cookie():
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[waf_token()])})

    timer = await harness.render(SHOP_URL)

    assert timer.notes()["clearance"] == "stored"
    assert harness.runtime.clearance.count() == 1
    assert harness.browser.injected == []


@pytest.mark.anyio
async def test_second_render_to_same_host_and_route_reuses_the_token():
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[waf_token()])})
    await harness.render(SHOP_URL)

    timer = await harness.render(OTHER_SHOP_URL)

    assert timer.notes()["clearance"] == "reused"
    assert harness.injected_names() == [["aws-waf-token"]]
    injected = harness.browser.injected[0][0]
    assert injected["value"] == TOKEN_VALUE
    assert injected["domain"] == ".example.com"
    assert injected["expires"] == NOW + 3600
    assert (injected["secure"], injected["httpOnly"], injected["sameSite"]) == (True, False, "Lax")


@pytest.mark.anyio
async def test_token_is_injected_into_the_new_context_only():
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[waf_token()])})
    await harness.render(SHOP_URL)

    await harness.render(OTHER_SHOP_URL)

    injected_context = harness.browser.cookie_jars["context-2"]
    assert [cookie["name"] for cookie in injected_context] == ["aws-waf-token"]


@pytest.mark.anyio
async def test_token_from_direct_route_is_not_used_via_home_proxy():
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[waf_token()])})
    await harness.render(SHOP_URL)

    timer = await harness.render(OTHER_SHOP_URL, use_proxy=True)

    assert timer.notes()["clearance"] == "none"
    assert harness.browser.injected == []


@pytest.mark.anyio
async def test_token_is_not_sent_to_a_lookalike_host():
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[waf_token()])})
    await harness.render(SHOP_URL)

    await harness.render(LOOKALIKE_URL)

    assert harness.browser.injected == []


@pytest.mark.anyio
async def test_non_clearance_cookies_are_never_stored():
    session_cookie = cdp_cookie("PHPSESSID", ".example.com", expires=NOW + 3600)
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[session_cookie])})

    timer = await harness.render(SHOP_URL)

    assert timer.notes()["clearance"] == "none"
    assert harness.runtime.clearance.count() == 0


@pytest.mark.anyio
async def test_render_ending_on_a_challenge_stores_nothing(short_readiness_budget):
    challenged = challenged_page()
    challenged.sets_cookies = [waf_token()]
    harness = await start_harness({SHOP_URL: challenged})

    with pytest.raises(TargetBlockedError):
        await harness.render(SHOP_URL, timeout_seconds=SHORT_BUDGET_SECONDS)

    assert harness.runtime.clearance.count() == 0


@pytest.mark.anyio
async def test_error_status_page_stores_nothing():
    harness = await start_harness({SHOP_URL: FakePage(status=403, sets_cookies=[waf_token()])})

    await harness.render(SHOP_URL)

    assert harness.runtime.clearance.count() == 0


@pytest.mark.anyio
async def test_rejected_token_is_dropped(short_readiness_budget):
    pages = {SHOP_URL: FakePage(sets_cookies=[waf_token()]), OTHER_SHOP_URL: challenged_page()}
    harness = await start_harness(pages)
    await harness.render(SHOP_URL)

    timer = PhaseTimer()
    with pytest.raises(TargetBlockedError):
        await harness.render(OTHER_SHOP_URL, timer, timeout_seconds=SHORT_BUDGET_SECONDS)

    assert timer.notes()["clearance"] == "dropped"
    assert harness.runtime.clearance.count() == 0


@pytest.mark.anyio
async def test_reuse_can_be_disabled():
    harness = await start_harness(
        {SHOP_URL: FakePage(sets_cookies=[waf_token()])}, CLEARANCE_REUSE="false"
    )
    await harness.render(SHOP_URL)

    timer = await harness.render(OTHER_SHOP_URL)

    assert harness.runtime.clearance is None
    assert "clearance" not in timer.notes()
    assert not any(method.startswith("Storage.") for method in harness.browser.sent_methods)


@pytest.mark.anyio
async def test_cdp_failure_during_inject_still_renders(caplog):
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[waf_token()])})
    await harness.render(SHOP_URL)
    harness.browser.fail_storage = True

    with caplog.at_level(logging.WARNING, logger="render.browser"):
        timer = await harness.render(OTHER_SHOP_URL)

    assert timer.notes()["clearance"] == "none"
    assert "Could not inject clearance cookies" in caplog.text
    assert TOKEN_VALUE not in caplog.text


@pytest.mark.anyio
async def test_cdp_failure_during_harvest_still_renders(caplog):
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[waf_token()])})
    harness.browser.fail_storage = True

    with caplog.at_level(logging.WARNING, logger="render.browser"):
        timer = await harness.render(SHOP_URL)

    assert timer.notes()["clearance"] == "none"
    assert "Could not read clearance cookies" in caplog.text
    assert harness.runtime.clearance.count() == 0


@pytest.mark.anyio
async def test_host_only_cookie_is_reinjected_as_host_only():
    host_only = cdp_cookie("cf_clearance", "shop.example.com", expires=NOW + 3600)
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[host_only])})
    await harness.render(SHOP_URL)

    await harness.render(OTHER_SHOP_URL)

    injected = harness.browser.injected[0][0]
    assert injected["url"] == "https://shop.example.com/"
    assert "domain" not in injected


@pytest.mark.anyio
async def test_partitioned_cookie_is_not_stored():
    partitioned = waf_token()
    partitioned["partitionKey"] = {
        "topLevelSite": "https://other.test",
        "hasCrossSiteAncestor": True,
    }
    harness = await start_harness({SHOP_URL: FakePage(sets_cookies=[partitioned])})

    await harness.render(SHOP_URL)

    assert harness.runtime.clearance.count() == 0
