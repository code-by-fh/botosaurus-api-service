"""Carries anti-bot clearance cookies between the ``ClearanceStore`` and one browser context.

Reuse is an optimisation, never a requirement: every CDP failure here is logged
and the render continues as if nothing had been stored. Cookie values are
never logged.
"""

import logging
from dataclasses import dataclass
from enum import Enum

from zendriver import cdp
from zendriver.core.connection import ProtocolException

from app.browser.session import ContextCookies
from app.scraping.clearance import ClearanceCookie, ClearanceStore, EgressRoute

log = logging.getLogger("render.browser")

DOMAIN_COOKIE_PREFIX = "."
SECURE_SCHEME = "https"
PLAIN_SCHEME = "http"


class ClearanceOutcome(Enum):
    """What happened to clearance cookies during one render (logged per request)."""

    NONE = "none"
    """Nothing was injected and nothing new was stored."""
    REUSED = "reused"
    """Stored cookies were injected and the render did not end on a challenge."""
    STORED = "stored"
    """Nothing was injected, but the render earned cookies that were stored."""
    DROPPED = "dropped"
    """Injected cookies were followed by an unresolved challenge and were forgotten."""


@dataclass(frozen=True)
class ClearanceScope:
    """Where a render goes: the egress route and the target host."""

    route: EgressRoute
    host: str


def to_cookie_param(cookie: ClearanceCookie) -> cdp.network.CookieParam:
    """The CDP form of ``cookie``, keeping host-only cookies host-only.

    Chrome turns any cookie set with ``domain`` into a domain cookie; a
    host-only cookie keeps its scope only when it is set through a URL.
    """
    host_only = not cookie.domain.startswith(DOMAIN_COOKIE_PREFIX)
    scheme = SECURE_SCHEME if cookie.secure else PLAIN_SCHEME
    return cdp.network.CookieParam(
        name=cookie.name,
        value=cookie.value,
        url=f"{scheme}://{cookie.domain}{cookie.path}" if host_only else None,
        domain=None if host_only else cookie.domain,
        path=cookie.path,
        secure=cookie.secure,
        http_only=cookie.http_only,
        same_site=cdp.network.CookieSameSite(cookie.same_site) if cookie.same_site else None,
        expires=None if cookie.expires is None else cdp.network.TimeSinceEpoch(cookie.expires),
    )


def from_cdp_cookie(cookie: cdp.network.Cookie) -> ClearanceCookie | None:
    """An engine-neutral copy, or ``None`` for a partitioned cookie.

    Partitioned (CHIPS) cookies are only valid inside their top-level site;
    re-injecting them unpartitioned would change their scope, so they are skipped.
    """
    if cookie.partition_key is not None:
        return None
    return ClearanceCookie(
        name=cookie.name,
        value=cookie.value,
        domain=cookie.domain,
        path=cookie.path,
        secure=cookie.secure,
        http_only=cookie.http_only,
        same_site=cookie.same_site.value if cookie.same_site else None,
        expires=None if cookie.session else cookie.expires,
    )


class ClearanceVisit:
    """Clearance handling for one render in one browser context."""

    def __init__(self, store: ClearanceStore, cookies: ContextCookies, scope: ClearanceScope):
        self._store = store
        self._cookies = cookies
        self._scope = scope
        self._injected = 0
        self._stored = 0
        self._dropped = False

    async def inject(self) -> None:
        """Set the stored cookies that match the target in the context; call before navigating."""
        matching = self._store.matching(self._scope.route, self._scope.host)
        if not matching:
            return
        try:
            await self._cookies.add([to_cookie_param(cookie) for cookie in matching])
        except ProtocolException as exc:
            self._warn("Could not inject clearance cookies", exc)
            return
        self._injected = len(matching)

    async def harvest(self) -> None:
        """Store the context's clearance cookies; call only after a render without challenge."""
        try:
            cookies = await self._cookies.read()
        except ProtocolException as exc:
            self._warn("Could not read clearance cookies", exc)
            return
        copies = (from_cdp_cookie(cookie) for cookie in cookies)
        self._stored = self._store.store(self._scope.route, filter(None, copies))

    def reject(self) -> None:
        """Forget the injected cookies because the site still showed its challenge."""
        if self._injected:
            self._store.forget(self._scope.route, self._scope.host)
            self._dropped = True

    @property
    def outcome(self) -> ClearanceOutcome:
        if self._dropped:
            return ClearanceOutcome.DROPPED
        if self._injected:
            return ClearanceOutcome.REUSED
        return ClearanceOutcome.STORED if self._stored else ClearanceOutcome.NONE

    def _warn(self, message: str, exc: ProtocolException) -> None:
        log.warning(
            "%s for %s (route=%s), continuing without reuse: %s",
            message,
            self._scope.host,
            self._scope.route.value,
            exc.message,
        )
