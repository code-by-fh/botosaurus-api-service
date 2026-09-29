"""Test doubles for the system boundaries: Chrome (via CDP), HTTP and DNS."""

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

from zendriver import cdp

from app.browser.page_loader import UPSTREAM_STATUS_SCRIPT
from app.config import Settings, load_settings
from app.errors import NavigationError
from app.fetch.http_fetcher import HttpFetchRequest, HttpPage

TEST_API_KEY = "test-key-app-one"
SECOND_API_KEY = "test-key-app-two"
PUBLIC_ADDRESS = "93.184.215.14"

LONG_ARTICLE = " ".join(f"sentence{index} explains the topic in detail" for index in range(60))
SERVER_RENDERED_HTML = (
    "<html><head><title>Article</title></head>"
    f"<body><main id='content'><h1>Headline</h1><p>{LONG_ARTICLE}</p></main></body></html>"
)
SPA_SHELL_HTML = (
    "<html><head><title>App</title><script src='/bundle.js'></script></head>"
    "<body><div id='root'></div></body></html>"
)
CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><div id='challenge-form'>Checking your browser</div></body></html>"
)


def make_settings(**overrides: str) -> Settings:
    """Settings from a minimal environment plus ``overrides`` (env-var names)."""
    env = {"API_KEYS": f"{TEST_API_KEY},{SECOND_API_KEY}", "QUEUE_TIMEOUT_SECONDS": "1"}
    env.update(overrides)
    return load_settings(env)


async def public_resolver(host: str) -> list[str]:
    return [PUBLIC_ADDRESS]


def stable_probe(text_length: int = 500, **changes) -> dict:
    probe = {
        "ready": "complete",
        "textLength": text_length,
        "nodeCount": 40,
        "requestCount": 12,
        "challenge": False,
        "found": True,
    }
    probe.update(changes)
    return probe


@dataclass
class FakePage:
    """What the fake browser shows for a URL: final DOM plus the probe sequence."""

    html: str = SERVER_RENDERED_HTML
    probes: list[dict] = field(default_factory=lambda: [stable_probe()])
    navigation_error: str | None = None
    hangs: bool = False
    status: int = 200

    def probe_at(self, index: int) -> dict:
        return self.probes[min(index, len(self.probes) - 1)]


class FakeTab:
    """Answers the CDP commands zendriver sends, the way Chrome would."""

    def __init__(self, browser: "FakeBrowser", context_id: str):
        self._browser = browser
        self.target = SimpleNamespace(browser_context_id=cdp.browser.BrowserContextID(context_id))
        self.url = "about:blank"
        self._probe_count = 0

    async def send(self, command):
        request = next(command)
        self._browser.sent_methods.append(request["method"])
        response = self._respond(request)
        try:
            command.send(response)
        except StopIteration as done:
            return done.value
        raise AssertionError("CDP command generator did not finish")

    def _respond(self, request: dict) -> dict:
        if request["method"] != "Page.navigate":
            return {}
        self.url = request["params"]["url"]
        error = self._browser.page_for(self.url).navigation_error
        return {"frameId": "frame-1", "errorText": error} if error else {"frameId": "frame-1"}

    async def evaluate(self, expression: str):
        page = self._browser.page_for(self.url)
        if expression == "location.href":
            return self.url
        if expression == UPSTREAM_STATUS_SCRIPT:
            return page.status
        if page.hangs:
            await asyncio.Event().wait()
        probe = page.probe_at(self._probe_count)
        self._probe_count += 1
        return probe

    async def get_content(self) -> str:
        return self._browser.page_for(self.url).html

    async def aclose(self) -> None:
        self._browser.closed_tabs += 1


class FakeConnection:
    def __init__(self, browser: "FakeBrowser"):
        self._browser = browser

    async def send(self, command):
        request = next(command)
        self._browser.sent_methods.append(request["method"])
        if request["method"] == "Target.disposeBrowserContext" and self._browser.fail_dispose:
            raise ConnectionError("websocket closed")
        try:
            command.send({})
        except StopIteration as done:
            return done.value


class FakeBrowser:
    """Minimal stand-in for ``zendriver.Browser``."""

    def __init__(self, pages: dict[str, FakePage]):
        self._pages = pages
        self.connection = FakeConnection(self)
        self.sent_methods: list[str] = []
        self.context_proxies: list[str | None] = []
        self.context_bypass_lists: list[list[str] | None] = []
        self.closed_tabs = 0
        self.fail_dispose = False
        self.fail_stop = False
        self.stopped = False

    def page_for(self, url: str) -> FakePage:
        return self._pages.get(url, FakePage())

    async def create_context(
        self, proxy_server: str | None = None, proxy_bypass_list: list[str] | None = None
    ) -> FakeTab:
        self.context_proxies.append(proxy_server)
        self.context_bypass_lists.append(proxy_bypass_list)
        return FakeTab(self, f"context-{len(self.context_proxies)}")

    async def stop(self) -> None:
        if self.fail_stop:
            raise ConnectionError("websocket already closed")
        self.stopped = True


class FakeLauncher:
    """Launcher that hands out ``FakeBrowser`` instances and remembers them."""

    def __init__(self, pages: dict[str, FakePage] | None = None):
        self.pages = pages if pages is not None else {}
        self.launched: list[FakeBrowser] = []
        self.specs: list = []

    async def __call__(self, spec) -> FakeBrowser:
        self.specs.append(spec)
        browser = FakeBrowser(self.pages)
        self.launched.append(browser)
        return browser


@dataclass
class FakeHttpFetcher:
    """Stand-in for the curl_cffi fetcher; serves canned responses per URL."""

    responses: dict[str, HttpPage] = field(default_factory=dict)
    requests: list[HttpFetchRequest] = field(default_factory=list)

    async def fetch(self, request: HttpFetchRequest) -> HttpPage:
        self.requests.append(request)
        if request.url not in self.responses:
            raise NavigationError(f"connection refused: {request.url}")
        return self.responses[request.url]

    async def close(self) -> None:
        return None


def http_page(html: str, url: str, status: int = 200) -> HttpPage:
    return HttpPage(
        status=status,
        content_type="text/html; charset=utf-8",
        html=html,
        final_url=url,
        truncated=False,
    )
