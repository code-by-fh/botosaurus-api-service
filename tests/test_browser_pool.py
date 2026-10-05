import asyncio
from types import SimpleNamespace

import pytest

import app.browser.worker as worker_module
from app.browser.blocking import BlockedResource, block_patterns
from app.browser.page_loader import HTML_LENGTH_SCRIPT, BrowserJob
from app.browser.pool import BrowserPool
from app.browser.session import LaunchSpec, SessionFactory, browser_args
from app.browser.worker import BrowserWorker
from app.egress import EgressGateway
from app.errors import NavigationError, RenderTimeoutError, ServiceBusyError
from app.url_guard import UrlGuard
from tests.fakes import SERVER_RENDERED_HTML, FakeLauncher, FakePage, make_settings, stable_probe

URL = "https://example.com/article"
PROXY_URL = "http://proxy.example:3128"


def job(url: str = URL, **changes) -> BrowserJob:
    values = {
        "url": url,
        "wait_for": None,
        "timeout_seconds": 5.0,
        "use_proxy": False,
        "block_resources": frozenset(),
    }
    values.update(changes)
    return BrowserJob(**values)


async def start_pool_with_egress(
    launcher: FakeLauncher, **env: str
) -> tuple[BrowserPool, EgressGateway]:
    settings = make_settings(**env)
    egress = EgressGateway(UrlGuard(allow_private=True), settings.browser.proxy_url)
    await egress.start()
    factory = SessionFactory(settings.browser, launcher, egress)
    workers = [BrowserWorker(factory) for _ in range(settings.browser.worker_count)]
    pool = BrowserPool(workers, settings.queue)
    await pool.start()
    return pool, egress


async def started_pool(launcher: FakeLauncher, **env: str) -> BrowserPool:
    pool, _ = await start_pool_with_egress(launcher, **env)
    return pool


async def wait_for_recycling(pool: BrowserPool) -> None:
    while pool.stats().recycling:
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_render_returns_dom_and_disposes_context():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1")

    page = await pool.render(job())

    browser = launcher.launched[0]
    assert page.html == SERVER_RENDERED_HTML
    assert page.final_url == URL
    assert page.stable is True
    assert "Target.disposeBrowserContext" in browser.sent_methods
    assert browser.closed_tabs == 1


def blocked_url_requests(launcher: FakeLauncher) -> list[dict]:
    commands = launcher.launched[0].tab_commands
    return [command for command in commands if command["method"] == "Network.setBlockedURLs"]


@pytest.mark.anyio
async def test_resource_blocking_is_only_applied_on_request():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1")

    await pool.render(job(block_resources=frozenset()))
    await pool.render(job(block_resources=frozenset({BlockedResource.IMAGE})))

    assert len(blocked_url_requests(launcher)) == 1


@pytest.mark.anyio
async def test_resource_blocking_sends_url_patterns_not_deprecated_substrings():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1")
    kinds = frozenset({BlockedResource.FONT, BlockedResource.IMAGE})

    await pool.render(job(block_resources=kinds))

    [request] = blocked_url_requests(launcher)
    expected = block_patterns(kinds)
    assert request["params"] == {"urlPatterns": [pattern.to_json() for pattern in expected]}


@pytest.mark.anyio
async def test_proxy_is_used_only_for_contexts_that_request_it():
    launcher = FakeLauncher()
    pool, egress = await start_pool_with_egress(launcher, MAX_WORKERS="1", HOME_PROXY=PROXY_URL)

    await pool.render(job(use_proxy=True))
    await pool.render(job(use_proxy=False))

    expected = [egress.url_for(use_proxy=True), egress.url_for(use_proxy=False)]
    assert launcher.launched[0].context_proxies == expected


@pytest.mark.anyio
async def test_browser_background_traffic_goes_through_direct_egress():
    launcher = FakeLauncher()
    _, egress = await start_pool_with_egress(launcher, MAX_WORKERS="1")

    assert launcher.specs[0].default_proxy == egress.url_for(use_proxy=False)


@pytest.mark.anyio
async def test_contexts_do_not_bypass_the_egress_for_local_addresses():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1")

    await pool.render(job())

    assert launcher.launched[0].context_bypass_lists == [["<-loopback>"]]


def test_chrome_is_launched_without_implicit_proxy_bypass():
    spec = LaunchSpec(make_settings().browser, default_proxy="http://127.0.0.1:9")

    assert "--proxy-bypass-list=<-loopback>" in browser_args(spec)


@pytest.mark.anyio
async def test_navigation_error_is_reported():
    launcher = FakeLauncher({URL: FakePage(navigation_error="net::ERR_NAME_NOT_RESOLVED")})
    pool = await started_pool(launcher, MAX_WORKERS="1")

    with pytest.raises(NavigationError, match="ERR_NAME_NOT_RESOLVED"):
        await pool.render(job())


@pytest.mark.anyio
async def test_browser_is_replaced_after_max_pages():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1", BROWSER_MAX_PAGES="2")

    await pool.render(job())
    await pool.render(job())
    await wait_for_recycling(pool)

    assert len(launcher.launched) == 2
    assert launcher.launched[0].stopped is True
    assert pool.stats().restarts == 1


@pytest.mark.anyio
async def test_browser_is_replaced_when_context_cannot_be_disposed():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1")
    launcher.launched[0].fail_dispose = True

    await pool.render(job())
    await wait_for_recycling(pool)

    assert len(launcher.launched) == 2


@pytest.mark.anyio
async def test_full_queue_is_rejected_immediately():
    launcher = FakeLauncher({URL: FakePage(probes=[stable_probe(ready="loading")])})
    pool = await started_pool(launcher, MAX_WORKERS="1", MAX_QUEUE_SIZE="0")
    blocking = asyncio.create_task(pool.render(job(timeout_seconds=1.0)))
    await asyncio.sleep(0)

    with pytest.raises(ServiceBusyError, match="queue is full"):
        await pool.render(job())
    await blocking


@pytest.mark.anyio
async def test_stats_reflect_busy_and_idle_workers():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="2")

    stats = pool.stats()

    assert (stats.total, stats.idle, stats.busy, stats.waiting) == (2, 2, 0, 0)


@pytest.mark.anyio
async def test_browser_is_replaced_after_max_age():
    launcher = FakeLauncher()
    settings = make_settings(BROWSER_MAX_AGE_SECONDS="600")
    egress = EgressGateway(UrlGuard(allow_private=True), None)
    await egress.start()
    now = [0.0]
    worker = BrowserWorker(SessionFactory(settings.browser, launcher, egress), clock=lambda: now[0])
    pool = BrowserPool([worker], settings.queue)
    await pool.start()

    now[0] = 600.0
    await pool.render(job())
    await wait_for_recycling(pool)

    assert len(launcher.launched) == 2


@pytest.mark.anyio
async def test_hanging_browser_times_out_and_is_replaced(monkeypatch):
    monkeypatch.setattr(worker_module, "HARD_DEADLINE_GRACE_SECONDS", 0.0)
    launcher = FakeLauncher({URL: FakePage(hangs=True)})
    pool = await started_pool(launcher, MAX_WORKERS="1")

    with pytest.raises(RenderTimeoutError, match="did not respond"):
        await pool.render(job(timeout_seconds=0.05))
    await wait_for_recycling(pool)

    assert len(launcher.launched) == 2


@pytest.mark.anyio
async def test_server_withholding_its_first_byte_times_out_without_restarting_chrome():
    tarpit = "https://tarpit.example/"
    launcher = FakeLauncher({tarpit: FakePage(navigation_hangs=True)})
    pool = await started_pool(launcher, MAX_WORKERS="1")

    with pytest.raises(RenderTimeoutError, match="did not start responding"):
        await pool.render(job(tarpit, timeout_seconds=0.05))
    await pool.render(job())

    assert len(launcher.launched) == 1
    assert launcher.launched[0].closed_tabs == 2


def evaluations(launcher: FakeLauncher) -> list[dict]:
    commands = launcher.launched[0].tab_commands
    return [command for command in commands if command["method"] == "Runtime.evaluate"]


@pytest.mark.anyio
async def test_no_evaluation_claims_a_user_gesture():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1")

    await pool.render(job())

    sent = evaluations(launcher)
    assert {command["params"]["expression"] for command in sent} >= {"location.href"}
    assert [command for command in sent if command["params"].get("userGesture")] == []


@pytest.mark.anyio
async def test_document_size_is_measured_in_an_isolated_world():
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1")

    await pool.render(job())

    sent = evaluations(launcher)
    size_checks = [c for c in sent if c["params"]["expression"] == HTML_LENGTH_SCRIPT]
    assert len(size_checks) == 1
    assert "contextId" in size_checks[0]["params"]


@pytest.mark.anyio
async def test_authenticated_proxy_is_reached_through_local_forwarder():
    launcher = FakeLauncher()
    pool = await started_pool(
        launcher, MAX_WORKERS="1", HOME_PROXY="http://user:secret@proxy.example:3128"
    )

    await pool.render(job(use_proxy=True))
    await pool.shutdown()

    context_proxy = launcher.launched[0].context_proxies[0]
    assert context_proxy.startswith("http://127.0.0.1:")
    assert "secret" not in context_proxy


class StubbornProcess:
    def __init__(self):
        self.killed = False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True


@pytest.mark.anyio
async def test_browser_that_fails_to_stop_is_killed_and_its_profile_removed(tmp_path):
    launcher = FakeLauncher()
    pool = await started_pool(launcher, MAX_WORKERS="1")
    browser = launcher.launched[0]
    profile = tmp_path / "profile"
    profile.mkdir()
    browser.fail_stop = True
    browser._process = StubbornProcess()
    browser.config = SimpleNamespace(uses_custom_data_dir=False, user_data_dir=str(profile))

    await pool.shutdown()

    assert browser._process.killed is True
    assert not profile.exists()
