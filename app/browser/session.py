"""One running Chrome instance, driven by zendriver.

Every render gets its own browser context (comparable to a fresh incognito
window): no storage, cache or cookies leak between requests of different
client apps, with exactly one exception. Allow-listed anti-bot clearance
cookies (``app.scraping.clearance``) are copied into a new context when an
earlier render on the same egress route earned them, for at most
``CLEARANCE_MAX_AGE_SECONDS``. They prove a solved challenge and carry no user
identity; no other cookie is ever carried over.

Each context is pointed at the egress proxy for its route, and the browser's
own background traffic goes through the direct egress proxy, so nothing leaves
Chrome unchecked.
"""

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import zendriver
from zendriver import cdp

from app.config import BrowserSettings
from app.egress import EgressGateway

log = logging.getLogger("render.browser")

WINDOW_SIZE_ARG = "--window-size=1920,1080"
HEADED_ONLY_ARGS = ("--start-maximized",)
# Chrome sends localhost, loopback and link-local destinations (169.254.0.0/16,
# which includes the cloud metadata service) around any configured proxy.
# This rule removes those implicit exceptions so they reach the egress guard.
NO_IMPLICIT_BYPASS = "<-loopback>"
CONTEXT_CLOSE_TIMEOUT_SECONDS = 5.0
STOP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class LaunchSpec:
    """Everything needed to start one Chrome process."""

    settings: BrowserSettings
    default_proxy: str


Launcher = Callable[[LaunchSpec], Awaitable[zendriver.Browser]]


def browser_args(spec: LaunchSpec) -> list[str]:
    """Extra Chrome switches on top of zendriver's defaults.

    Deliberately short: every unusual switch is a potential fingerprint.
    WebRTC is already restricted to proxied connections by zendriver's default
    ``disable_webrtc`` so the VPS address cannot leak through STUN.
    """
    args = [
        WINDOW_SIZE_ARG,
        f"--proxy-server={spec.default_proxy}",
        f"--proxy-bypass-list={NO_IMPLICIT_BYPASS}",
    ]
    if not spec.settings.headless:
        args.extend(HEADED_ONLY_ARGS)
    return args


async def launch_chrome(spec: LaunchSpec) -> zendriver.Browser:
    """Start a Chrome process with a temporary profile (removed on stop)."""
    config = zendriver.Config(
        headless=spec.settings.headless,
        sandbox=spec.settings.sandbox,
        lang=spec.settings.locale,
        browser_executable_path=spec.settings.executable_path,
        browser_args=browser_args(spec),
    )
    return await zendriver.Browser.create(config)


class ContextCookies:
    """Cookie access to exactly one browser context over the browser's CDP connection.

    The Storage commands run on the browser endpoint with an explicit context
    id; without one Chrome would use its default context, shared by all tabs.
    """

    def __init__(self, browser: zendriver.Browser, context_id: cdp.browser.BrowserContextID):
        self._browser = browser
        self._context_id = context_id

    async def add(self, cookies: list[cdp.network.CookieParam]) -> None:
        """Set ``cookies`` in the context.

        :raises ProtocolException: if Chrome rejects the command or the connection is gone.
        """
        command = cdp.storage.set_cookies(cookies, browser_context_id=self._context_id)
        await self._browser.connection.send(command)

    async def read(self) -> list[cdp.network.Cookie]:
        """All cookies of the context.

        :raises ProtocolException: if Chrome rejects the command or the connection is gone.
        """
        command = cdp.storage.get_cookies(browser_context_id=self._context_id)
        return await self._browser.connection.send(command)


class BrowserSession:
    """A started browser that hands out isolated tabs."""

    def __init__(self, browser: zendriver.Browser, egress: EgressGateway):
        self._browser = browser
        self._egress = egress
        self._healthy = True

    @property
    def healthy(self) -> bool:
        """False once an operation left the browser in an unknown state."""
        return self._healthy and not self._browser.stopped

    def mark_unhealthy(self) -> None:
        self._healthy = False

    async def open_tab(self, use_proxy: bool) -> zendriver.Tab:
        """Open ``about:blank`` in a new, empty browser context on the chosen route."""
        proxy = self._egress.url_for(use_proxy)
        if use_proxy:
            log.info("Opening browser tab via HOME_PROXY (%s)", proxy)
        else:
            log.debug("Opening tab with direct egress (%s)", proxy)
        return await self._browser.create_context(
            proxy_server=proxy, proxy_bypass_list=[NO_IMPLICIT_BYPASS]
        )

    def context_cookies(self, tab: zendriver.Tab) -> ContextCookies | None:
        """Cookie access to the context of ``tab``; ``None`` if it has no own context."""
        context_id = tab.target.browser_context_id if tab.target else None
        return None if context_id is None else ContextCookies(self._browser, context_id)

    async def close_tab(self, tab: zendriver.Tab) -> None:
        """Dispose the tab's browser context, dropping all of its state.

        :raises TimeoutError: if Chrome does not confirm within the close timeout.
        """
        context_id = tab.target.browser_context_id if tab.target else None
        async with asyncio.timeout(CONTEXT_CLOSE_TIMEOUT_SECONDS):
            if context_id is not None:
                await self._browser.connection.send(cdp.target.dispose_browser_context(context_id))
            await tab.aclose()

    async def stop(self) -> None:
        """Terminate Chrome and remove its profile, whatever state the browser is in."""
        # Any failure here (timeout, broken websocket) must still end the
        # process: the worker forgets this session afterwards, so a surviving
        # Chrome would leak memory and its profile would fill /tmp.
        try:
            async with asyncio.timeout(STOP_TIMEOUT_SECONDS):
                await self._browser.stop()
        except Exception:
            log.warning("Browser did not stop cleanly, killing it", exc_info=True)
            self._kill()
            self._remove_profile()

    def _kill(self) -> None:
        # zendriver exposes no public kill; its Popen handle is the only way to
        # end a process whose CDP connection is wedged.
        process = getattr(self._browser, "_process", None)
        if process is not None and process.poll() is None:
            process.kill()

    def _remove_profile(self) -> None:
        config = self._browser.config
        if not config.uses_custom_data_dir:
            shutil.rmtree(config.user_data_dir, ignore_errors=True)


class SessionFactory:
    """Starts browser sessions wired to the egress gateway."""

    def __init__(self, settings: BrowserSettings, launcher: Launcher, egress: EgressGateway):
        self.settings = settings
        self._launcher = launcher
        self._egress = egress

    async def start(self) -> BrowserSession:
        spec = LaunchSpec(self.settings, default_proxy=self._egress.url_for(use_proxy=False))
        log.debug(
            "Launching browser: headless=%s sandbox=%s default_proxy=%s",
            self.settings.headless,
            self.settings.sandbox,
            spec.default_proxy,
        )
        browser = await self._launcher(spec)
        return BrowserSession(browser, self._egress)
