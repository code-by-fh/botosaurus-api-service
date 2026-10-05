"""Test doubles for the system boundaries: Chrome (via CDP), HTTP and DNS."""

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

from zendriver import cdp
from zendriver.core.connection import ProtocolException

from app.browser.page_loader import HTML_LENGTH_SCRIPT, UPSTREAM_STATUS_SCRIPT
from app.config import Settings, load_settings
from app.errors import NavigationError, ServiceError
from app.fetch.http_fetcher import HttpFetchRequest, HttpPage

# Long enough for the MIN_API_KEY_LENGTH check of the configuration.
TEST_API_KEY = "test-key-app-one-0123456789abcdefghij"
SECOND_API_KEY = "test-key-app-two-0123456789abcdefghij"
PUBLIC_ADDRESS = "93.184.215.14"

LONG_ARTICLE = " ".join(f"sentence{index} explains the topic in detail" for index in range(60))
SERVER_RENDERED_HTML = (
    "<html><head><title>Article</title></head>"
    f"<body><main id='content'><h1>Headline</h1><p>{LONG_ARTICLE}</p></main></body></html>"
)


def article_html(url: str) -> str:
    """A server-rendered page whose text differs per URL, as distinct pages of a site do."""
    return SERVER_RENDERED_HTML.replace("Headline", f"Headline of {url}")


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
    # Late-content observation is off unless a test turns it on: it keeps tabs open after
    # the response, which only the tests about it should have to account for.
    env = {
        "API_KEYS": f"{TEST_API_KEY},{SECOND_API_KEY}",
        "QUEUE_TIMEOUT_SECONDS": "1",
        "LATE_CONTENT_OBSERVE_SECONDS": "0",
    }
    env.update(overrides)
    return load_settings(env)


async def public_resolver(host: str) -> list[str]:
    return [PUBLIC_ADDRESS]


def cdp_cookie(name: str, domain: str, expires: float | None = None, value: str = "v") -> dict:
    """A cookie as Chrome reports it in ``Storage.getCookies``; ``None`` expiry = session."""
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "size": len(name) + len(value),
        "httpOnly": False,
        "secure": True,
        "session": expires is None,
        "priority": "Medium",
        "sourceScheme": "Secure",
        "sourcePort": 443,
        "expires": -1 if expires is None else expires,
        "sameSite": "Lax",
    }


def _cookie_from_param(param: dict) -> dict:
    # Chrome turns a url-scoped cookie into a host-only cookie of that host.
    domain = param.get("domain") or param["url"].split("/")[2]
    cookie = cdp_cookie(param["name"], domain, param.get("expires"), param["value"])
    cookie.update(path=param.get("path", "/"), secure=param.get("secure", False))
    cookie.update(httpOnly=param.get("httpOnly", False), sameSite=param.get("sameSite"))
    return cookie


def stable_probe(growth: int = 0, **changes) -> dict:
    """What the readiness observer script reports; a changed ``growth`` means new content."""
    probe = {
        "ready": "complete",
        "growth": growth,
        "challenge": False,
        "found": True,
        "placeholders": False,
    }
    probe.update(changes)
    return probe


def restless_probes() -> list[dict]:
    """A page whose content grows on every poll and never settles."""
    return [stable_probe(growth=count) for count in range(1, 100_000)]


class ManualClock:
    """A clock that only moves when a test (or a fake) advances it."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CdpEvents:
    """Event handler registry with zendriver's ``add_handler``/``remove_handlers`` API.

    ``emit`` awaits each handler directly, so a test sees its effect deterministically.
    """

    def __init__(self):
        self.handlers: dict[type, list] = {}
        # Every event type a handler was ever registered for, also after removal.
        self.handlers_seen: set[type] = set()

    def add_handler(self, event_type: type, handler) -> None:
        self.handlers.setdefault(event_type, []).append(handler)
        self.handlers_seen.add(event_type)

    def remove_handlers(self, event_type: type | None = None, handler=None) -> None:
        registered = self.handlers.get(event_type, [])
        if handler in registered:
            registered.remove(handler)

    async def emit(self, event) -> None:
        for handler in list(self.handlers.get(type(event), [])):
            await handler(event)


def request_sent(request_id: str, url: str, resource_type: str = "XHR"):
    """``Network.requestWillBeSent`` as Chrome reports it."""
    return cdp.network.RequestWillBeSent.from_json(
        {
            "requestId": request_id,
            "loaderId": "loader-1",
            "documentURL": url,
            "request": {
                "url": url,
                "method": "GET",
                "headers": {},
                "initialPriority": "High",
                "referrerPolicy": "strict-origin-when-cross-origin",
            },
            "timestamp": 1.0,
            "wallTime": 1.0,
            "initiator": {"type": "script"},
            "redirectHasExtraInfo": False,
            "type": resource_type,
            "frameId": "frame-1",
        }
    )


def loading_finished(request_id: str):
    return cdp.network.LoadingFinished.from_json(
        {"requestId": request_id, "timestamp": 2.0, "encodedDataLength": 100}
    )


def loading_failed(request_id: str, resource_type: str = "XHR"):
    return cdp.network.LoadingFailed.from_json(
        {"requestId": request_id, "timestamp": 2.0, "type": resource_type, "errorText": "net::ERR"}
    )


def response_received(request_id: str, resource_type: str):
    return cdp.network.ResponseReceived.from_json(
        {
            "requestId": request_id,
            "loaderId": "loader-1",
            "timestamp": 2.0,
            "type": resource_type,
            "response": {
                "url": "https://shop.example/stream",
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "text/event-stream",
                "charset": "",
                "connectionReused": False,
                "connectionId": 1,
                "encodedDataLength": 0,
                "securityState": "secure",
            },
            "hasExtraInfo": False,
        }
    )


@dataclass
class FakePage:
    """What the fake browser shows for a URL: final DOM, observations and network events."""

    html: str = SERVER_RENDERED_HTML
    probes: list[dict] = field(default_factory=lambda: [stable_probe()])
    navigation_error: str | None = None
    hangs: bool = False
    status: int = 200
    sets_cookies: list[dict] = field(default_factory=list)
    # CDP events the tab emits right before serving the observation with that index.
    events: dict[int, list] = field(default_factory=dict)
    # DOM served once the tab has answered more than ``late_html_after_probe`` observations.
    late_html: str | None = None
    late_html_after_probe: int = 0
    # Observations from this index on wait for ``gate`` (lets a test hold a tab mid-render).
    gate: asyncio.Event | None = None
    gate_from_probe: int = 0
    # How far each observation moves the launcher's manual clock, if it has one.
    probe_seconds: float = 0.125
    # The page breaks the size check (e.g. by overriding the outerHTML getter).
    size_check_fails: bool = False
    # Page.navigate never commits, like a server that withholds its first byte.
    navigation_hangs: bool = False

    def probe_at(self, index: int) -> dict:
        return self.probes[min(index, len(self.probes) - 1)]

    def html_after(self, probes_served: int) -> str:
        if self.late_html is not None and probes_served > self.late_html_after_probe:
            return self.late_html
        return self.html


MAIN_FRAME_ID = "frame-1"
ISOLATED_CONTEXT_ID = 7


def _main_frame_tree() -> dict:
    return {"frameTree": {"frame": {"id": MAIN_FRAME_ID}}}


def _evaluation_result(value) -> dict:
    return {"result": {"type": "object", "value": value}}


async def _finish_command(command, respond):
    request = next(command)
    response = await respond(request)
    try:
        command.send(response)
    except StopIteration as done:
        return done.value
    raise AssertionError("CDP command generator did not finish")


class FakeTab(CdpEvents):
    """Answers the CDP commands zendriver sends, the way Chrome would."""

    def __init__(self, browser: "FakeBrowser", context_id: str):
        super().__init__()
        self._browser = browser
        self.context_id = context_id
        self.target = SimpleNamespace(browser_context_id=cdp.browser.BrowserContextID(context_id))
        self.url = "about:blank"
        self._probe_count = 0

    async def send(self, command):
        return await _finish_command(command, self._respond)

    async def _respond(self, request: dict) -> dict:
        self._browser.sent_methods.append(request["method"])
        self._browser.tab_commands.append(request)
        method = request["method"]
        if method == "Page.navigate":
            return await self._navigate(request)
        if method == "Page.getFrameTree":
            return _main_frame_tree()
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": ISOLATED_CONTEXT_ID}
        if method == "Runtime.evaluate":
            return await self._evaluation(request["params"]["expression"])
        return {}

    async def _evaluation(self, expression: str) -> dict:
        page = self._browser.page_for(self.url)
        if expression == HTML_LENGTH_SCRIPT:
            return self._html_length(page)
        if expression == "location.href":
            return _evaluation_result(self.url)
        if expression == UPSTREAM_STATUS_SCRIPT:
            return _evaluation_result(page.status)
        return _evaluation_result(await self._next_probe())

    def _html_length(self, page: FakePage) -> dict:
        if page.size_check_fails:
            return SCRIPT_ERROR_RESULT
        html = page.html_after(self._probe_count)
        return {"result": {"type": "number", "value": len(html)}}

    async def _navigate(self, request: dict) -> dict:
        self.url = request["params"]["url"]
        page = self._browser.page_for(self.url)
        if page.navigation_hangs:
            await asyncio.Event().wait()
        self._browser.cookie_jar(self.context_id).extend(page.sets_cookies)
        response = {"frameId": MAIN_FRAME_ID}
        if page.navigation_error:
            response["errorText"] = page.navigation_error
        return response

    async def _next_probe(self) -> dict:
        page = self._browser.page_for(self.url)
        if page.hangs:
            await asyncio.Event().wait()
        if page.gate is not None and self._probe_count >= page.gate_from_probe:
            await page.gate.wait()
        for event in page.events.get(self._probe_count, []):
            await self.emit(event)
        probe = page.probe_at(self._probe_count)
        self._probe_count += 1
        if self._browser.clock is not None:
            self._browser.clock.advance(page.probe_seconds)
        if isinstance(probe, Exception):
            raise probe
        return probe

    async def get_content(self) -> str:
        return self._browser.page_for(self.url).html_after(self._probe_count)

    async def aclose(self) -> None:
        self._browser.closed_tabs += 1


# What Chrome answers when the evaluated script throws.
SCRIPT_ERROR_RESULT = {
    "result": {"type": "object", "subtype": "error"},
    "exceptionDetails": {
        "exceptionId": 1,
        "text": "Uncaught",
        "lineNumber": 0,
        "columnNumber": 0,
    },
}
# An observation that replaces the document: the isolated world is gone afterwards.
NAVIGATED = "navigated"
POLL_STEP_SECONDS = 0.125


class ScriptedTab(CdpEvents):
    """A tab whose readiness observations follow a script, on a manual clock.

    Every observation advances ``clock`` by ``step`` and first emits the CDP events
    scheduled for its index in ``events``. The last observation repeats.
    """

    def __init__(self, observations: list, events: dict[int, list] | None = None):
        super().__init__()
        self.observations = observations
        self.events = events or {}
        self.clock = ManualClock()
        self.step = POLL_STEP_SECONDS
        self.calls = 0
        self.worlds_created = 0
        self.main_world_calls = 0
        self.world_fails = False
        self.script_fails = False
        self._context: int | None = None

    async def send(self, command):
        return await _finish_command(command, self._respond)

    async def _respond(self, request: dict) -> dict:
        method = request["method"]
        if method == "Page.getFrameTree":
            return _main_frame_tree()
        if method == "Page.createIsolatedWorld":
            return self._create_world()
        if method == "Runtime.evaluate":
            if "contextId" not in request["params"]:
                self.main_world_calls += 1
                return _evaluation_result(await self._observe())
            if request["params"].get("contextId") != self._context:
                raise ProtocolException({"code": -32000, "message": "Cannot find context"})
            if self.script_fails:
                return SCRIPT_ERROR_RESULT
            return _evaluation_result(await self._observe())
        raise AssertionError(f"unexpected CDP command {method}")

    def _create_world(self) -> dict:
        if self.world_fails:
            raise ProtocolException({"code": -32000, "message": "No frame for given id"})
        self.worlds_created += 1
        self._context = ISOLATED_CONTEXT_ID + self.worlds_created
        return {"executionContextId": self._context}

    async def _observe(self):
        index = self.calls
        self.calls += 1
        self.clock.advance(self.step)
        for event in self.events.get(index, []):
            await self.emit(event)
        observation = self.observations[min(index, len(self.observations) - 1)]
        if observation == NAVIGATED:
            self._context = None
            raise ProtocolException({"code": -32000, "message": "Execution context was destroyed"})
        if isinstance(observation, Exception):
            raise observation
        return observation


class FakeConnection:
    def __init__(self, browser: "FakeBrowser"):
        self._browser = browser

    async def send(self, command):
        request = next(command)
        self._browser.sent_methods.append(request["method"])
        if request["method"] == "Target.disposeBrowserContext" and self._browser.fail_dispose:
            raise ConnectionError("websocket closed")
        response = self._respond(request)
        try:
            command.send(response)
        except StopIteration as done:
            return done.value

    def _respond(self, request: dict) -> dict:
        if not request["method"].startswith("Storage."):
            return {}
        if self._browser.fail_storage:
            raise ProtocolException({"code": -32000, "message": "Storage failed"})
        jar = self._browser.cookie_jar(request["params"]["browserContextId"])
        if request["method"] == "Storage.getCookies":
            return {"cookies": list(jar)}
        self._browser.injected.append(request["params"]["cookies"])
        jar.extend(_cookie_from_param(param) for param in request["params"]["cookies"])
        return {}


class FakeBrowser:
    """Minimal stand-in for ``zendriver.Browser``."""

    def __init__(self, pages: dict[str, FakePage], clock: ManualClock | None = None):
        self._pages = pages
        self.clock = clock
        self.connection = FakeConnection(self)
        self.sent_methods: list[str] = []
        # Full requests sent to tabs, so tests can check the parameters Chrome would see.
        self.tab_commands: list[dict] = []
        self.context_proxies: list[str | None] = []
        self.context_bypass_lists: list[list[str] | None] = []
        self.closed_tabs = 0
        self.tabs: list[FakeTab] = []
        self.fail_dispose = False
        self.fail_stop = False
        self.fail_storage = False
        self.stopped = False
        self.cookie_jars: dict[str, list[dict]] = {}
        self.injected: list[list[dict]] = []

    def cookie_jar(self, context_id: str) -> list[dict]:
        """The cookies of one browser context, like Chrome keeps them apart."""
        return self.cookie_jars.setdefault(context_id, [])

    def page_for(self, url: str) -> FakePage:
        return self._pages.get(url, FakePage())

    async def create_context(
        self, proxy_server: str | None = None, proxy_bypass_list: list[str] | None = None
    ) -> FakeTab:
        self.context_proxies.append(proxy_server)
        self.context_bypass_lists.append(proxy_bypass_list)
        tab = FakeTab(self, f"context-{len(self.context_proxies)}")
        self.tabs.append(tab)
        return tab

    async def stop(self) -> None:
        if self.fail_stop:
            raise ConnectionError("websocket already closed")
        self.stopped = True


class FakeLauncher:
    """Launcher that hands out ``FakeBrowser`` instances and remembers them.

    :param clock: manual clock the browsers' tabs advance on every observation.
    """

    def __init__(self, pages: dict[str, FakePage] | None = None, clock: ManualClock | None = None):
        self.pages = pages if pages is not None else {}
        self.clock = clock
        self.launched: list[FakeBrowser] = []
        self.specs: list = []

    async def __call__(self, spec) -> FakeBrowser:
        self.specs.append(spec)
        browser = FakeBrowser(self.pages, self.clock)
        self.launched.append(browser)
        return browser


@dataclass
class FakeHttpFetcher:
    """Stand-in for the curl_cffi fetcher; serves canned responses per URL."""

    responses: dict[str, HttpPage] = field(default_factory=dict)
    requests: list[HttpFetchRequest] = field(default_factory=list)
    fail_close: bool = False
    closed: bool = False
    # Errors raised for a URL instead of a response, e.g. a redirect to a forbidden host.
    failures: dict[str, ServiceError] = field(default_factory=dict)
    # A manual clock each fetch advances by ``fetch_seconds``, like a slow server.
    clock: ManualClock | None = None
    fetch_seconds: float = 0.0

    async def fetch(self, request: HttpFetchRequest) -> HttpPage:
        self.requests.append(request)
        if self.clock is not None:
            self.clock.advance(self.fetch_seconds)
        if request.url in self.failures:
            raise self.failures[request.url]
        if request.url not in self.responses:
            raise NavigationError(f"connection refused: {request.url}")
        return self.responses[request.url]

    async def close(self) -> None:
        if self.fail_close:
            raise ConnectionError("curl session already gone")
        self.closed = True


def http_page(html: str, url: str, status: int = 200) -> HttpPage:
    return HttpPage(
        status=status,
        content_type="text/html; charset=utf-8",
        html=html,
        final_url=url,
        truncated=False,
    )
