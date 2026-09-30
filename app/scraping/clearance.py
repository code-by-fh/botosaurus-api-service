"""Short-lived store of anti-bot clearance cookies, shared between browser renders.

A site behind a bot challenge sets a clearance cookie once the challenge is
solved. Without it every render in a fresh browser context pays for the
challenge again. This store keeps only those cookies (see
``CLEARANCE_ALLOW_LIST``) so the next render to the same site can present the
token instead.

Invariants:

- Nothing but allow-listed clearance cookies is ever stored: no session,
  login, consent or tracking cookies.
- Entries are keyed by egress route. Vendors bind a solved challenge to the
  client IP, so a token earned on one route must never be sent from the other.
- An entry lives until the cookie's own expiry or ``max_age_seconds``,
  whichever comes first, and the store holds at most ``capacity`` entries.

All methods are synchronous and never await, so concurrent requests on the
event loop cannot interleave inside one of them; no lock is needed.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

Clock = Callable[[], float]

DEFAULT_CAPACITY = 512
DOMAIN_SEPARATOR = "."


@dataclass(frozen=True)
class ClearanceAllowList:
    """Cookie names that prove a solved bot challenge: exact names plus name prefixes."""

    names: frozenset[str]
    prefixes: tuple[str, ...]


CLEARANCE_ALLOW_LIST = ClearanceAllowList(
    names=frozenset(
        {
            # AWS WAF: set by the challenge/CAPTCHA JavaScript once solved.
            "aws-waf-token",
            # Cloudflare: issued after a passed managed or JS challenge.
            "cf_clearance",
            # DataDome: the device check result the edge module validates.
            "datadome",
            # Imperva Advanced Bot Protection (formerly Distil): the solved-challenge token.
            "reese84",
            # HUMAN/PerimeterX Bot Defender: risk token and the visitor id it is bound to.
            "_px3",
            "_pxvid",
            # Akamai Bot Manager: validated sensor token and the session it is bound to.
            "_abck",
            "bm_sz",
        }
    ),
    prefixes=(
        # Imperva Incapsula: session and visitor cookies carry the site id as a suffix
        # and together identify a client that passed the Incapsula challenge.
        "incap_ses_",
        "visid_incap_",
    ),
)


class EgressRoute(Enum):
    """The egress IP a render leaves through; clearance is only valid for one of them."""

    DIRECT = "direct"
    HOME_PROXY = "home-proxy"


def route_for(use_proxy: bool) -> EgressRoute:
    """The route a request with ``use_proxy`` takes."""
    return EgressRoute.HOME_PROXY if use_proxy else EgressRoute.DIRECT


def is_clearance_cookie(name: str) -> bool:
    """True only for allow-listed clearance cookie names (case-sensitive, like cookies)."""
    allowed = CLEARANCE_ALLOW_LIST
    return name in allowed.names or name.startswith(allowed.prefixes)


def domain_matches(cookie_domain: str, host: str) -> bool:
    """Whether a browser would send a cookie of ``cookie_domain`` to ``host``.

    A leading dot marks a domain cookie (host and all subdomains); without it
    the cookie is host-only and matches that exact host alone.
    """
    domain = cookie_domain.lower()
    host = host.lower().rstrip(DOMAIN_SEPARATOR)
    if not domain.startswith(DOMAIN_SEPARATOR):
        return host == domain
    bare = domain[1:]
    return host == bare or host.endswith(domain)


@dataclass(frozen=True)
class ClearanceCookie:
    """An engine-neutral copy of one cookie.

    ``expires`` is in seconds since the epoch, ``None`` for a session cookie.
    The value is left out of ``repr`` so a token never ends up in a log line.
    """

    name: str
    value: str = field(repr=False)
    domain: str
    path: str
    secure: bool
    http_only: bool
    same_site: str | None
    expires: float | None


EntryKey = tuple[EgressRoute, str, str, str]


@dataclass(frozen=True)
class _Entry:
    cookie: ClearanceCookie
    valid_until: float


class ClearanceStore:
    """Bounded in-memory store of clearance cookies per egress route and cookie domain."""

    def __init__(
        self, max_age_seconds: float, clock: Clock = time.time, capacity: int = DEFAULT_CAPACITY
    ):
        """:param clock: wall clock in epoch seconds, the unit of cookie expiry."""
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._capacity = capacity
        self._entries: OrderedDict[EntryKey, _Entry] = OrderedDict()

    def store(self, route: EgressRoute, cookies: Iterable[ClearanceCookie]) -> int:
        """Keep the allow-listed, unexpired ``cookies`` for ``route``.

        :return: how many cookies were stored; all others are ignored.
        """
        stored = 0
        for cookie in cookies:
            if is_clearance_cookie(cookie.name) and self._put(route, cookie):
                stored += 1
        return stored

    def matching(self, route: EgressRoute, host: str) -> list[ClearanceCookie]:
        """Unexpired cookies of ``route`` that a browser would send to ``host``."""
        self._purge_expired()
        return [entry.cookie for key, entry in self._entries.items() if _selects(key, route, host)]

    def forget(self, route: EgressRoute, host: str) -> int:
        """Drop the cookies of ``route`` that match ``host``, e.g. after the site rejected them.

        :return: how many entries were dropped.
        """
        keys = [key for key in self._entries if _selects(key, route, host)]
        for key in keys:
            del self._entries[key]
        return len(keys)

    def count(self) -> int:
        """Number of unexpired entries."""
        self._purge_expired()
        return len(self._entries)

    def _put(self, route: EgressRoute, cookie: ClearanceCookie) -> bool:
        valid_until = self._valid_until(cookie)
        if valid_until <= self._clock():
            return False
        key = (route, cookie.domain.lower(), cookie.name, cookie.path)
        self._entries.pop(key, None)
        self._entries[key] = _Entry(cookie, valid_until)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return True

    def _valid_until(self, cookie: ClearanceCookie) -> float:
        capped = self._clock() + self._max_age_seconds
        return capped if cookie.expires is None else min(capped, cookie.expires)

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.valid_until <= now]
        for key in expired:
            del self._entries[key]


def _selects(key: EntryKey, route: EgressRoute, host: str) -> bool:
    entry_route, domain, _, _ = key
    return entry_route is route and domain_matches(domain, host)
