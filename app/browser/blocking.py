"""Which requests the browser skips when a caller asks for resource blocking, and the
shipped tracker domain list that readiness ignores.

Patterns go to Chrome through ``Network.setBlockedURLs(urlPatterns=...)``, which uses the
URLPattern syntax and applies the first matching pattern. The deprecated ``urls`` parameter
is not used: Chrome matched its patterns as substrings, so ``*.ico`` also blocked the
scripts of a host like ``shop.icon.de``.

Only request kinds whose loss rarely hides page text can be blocked. WebSockets cannot:
Chrome does not reliably block their handshakes, readiness does not wait for them anyway,
and the data they carry is often the content itself (see ADR 0004).

Trackers are not blocked: readiness ignores their requests instead
(``network_activity.InflightTracker``, ADR 0005), which gives the speed without the
detection risk of a client that never loads them.
"""

import re
from collections.abc import Iterable
from enum import StrEnum
from functools import cache
from pathlib import Path

from zendriver import cdp

from app.config import ConfigError


class BlockedResource(StrEnum):
    """A kind of request the caller may ask the browser to skip."""

    IMAGE = "image"
    FONT = "font"
    MEDIA = "media"
    STYLESHEET = "stylesheet"


IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "ico", "bmp")
FONT_EXTENSIONS = ("woff", "woff2", "ttf", "otf", "eot")
MEDIA_EXTENSIONS = (
    "mp4",
    "m4v",
    "mov",
    "webm",
    "ogv",
    "mp3",
    "m4a",
    "aac",
    "ogg",
    "oga",
    "opus",
    "wav",
    "flac",
)
STYLESHEET_EXTENSIONS = ("css",)
EXTENSIONS_BY_KIND = {
    BlockedResource.IMAGE: IMAGE_EXTENSIONS,
    BlockedResource.FONT: FONT_EXTENSIONS,
    BlockedResource.MEDIA: MEDIA_EXTENSIONS,
    BlockedResource.STYLESHEET: STYLESHEET_EXTENSIONS,
}
# Anti-bot and captcha vendors. Blocking any of their requests (even an image or a
# stylesheet of a challenge) turns a solvable challenge into a hard block.
NEVER_BLOCKED_DOMAINS = frozenset(
    {
        "challenges.cloudflare.com",
        "datadome.co",
        "captcha-delivery.com",
        "perimeterx.net",
        "px-cdn.net",
        "px-cloud.net",
        "awswaf.com",
        "hcaptcha.com",
        "recaptcha.net",
        "arkoselabs.com",
        "funcaptcha.com",
        "incapsula.com",
        "imperva.com",
    }
)
# reCAPTCHA lives on Google's main hosts and Cloudflare serves its challenge platform from
# the protected site's own host, so these need path-scoped entries.
NEVER_BLOCKED_PATH_PATTERNS = (
    "*://www.google.com:*/recaptcha/*",
    "*://www.gstatic.com:*/recaptcha/*",
    "*://*:*/cdn-cgi/challenge-platform/*",
)
TRACKER_LIST_PATH = Path(__file__).resolve().parent / "data" / "tracker_domains.txt"
COMMENT_PREFIX = "#"
NO_BLOCKING = "none"
DOMAIN_SYNTAX = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)

TrackerDomains = tuple[str, ...]


def extension_pattern(extension: str) -> str:
    """URLPattern for any URL whose path ends in ``.extension``, with or without a query.

    The query is spelled out as ``?*`` rather than left to URLPattern's defaults, so a
    versioned asset such as ``app.css?v=3`` is matched whatever the default is.
    """
    return f"*://*:*/*.{extension}?*"


def domain_patterns(domain: str) -> tuple[str, str]:
    """URLPatterns for every URL on ``domain`` and its subdomains, on any port.

    The path is ``/*`` without an explicit query: ``/*?*`` would parse as an optional
    wildcard, and an unspecified search component matches any query.
    """
    return f"*://{domain}:*/*", f"*://*.{domain}:*/*"


def _domains_patterns(domains: Iterable[str]) -> tuple[str, ...]:
    return tuple(pattern for domain in domains for pattern in domain_patterns(domain))


NEVER_BLOCKED_PATTERNS = (
    _domains_patterns(sorted(NEVER_BLOCKED_DOMAINS)) + NEVER_BLOCKED_PATH_PATTERNS
)


def block_patterns(kinds: frozenset[BlockedResource]) -> list[cdp.network.BlockPattern]:
    """Patterns for ``Network.setBlockedURLs``: the allow-list first, then what to block.

    :param kinds: what the caller asked to skip; empty means no blocking at all.
    :return: an empty list when ``kinds`` is empty.
    """
    if not kinds:
        return []
    allowed = [cdp.network.BlockPattern(url_pattern=p, block=False) for p in NEVER_BLOCKED_PATTERNS]
    blocked = [cdp.network.BlockPattern(url_pattern=p, block=True) for p in _blocked(kinds)]
    return allowed + blocked


def _blocked(kinds: frozenset[BlockedResource]) -> list[str]:
    extensions = [ext for kind in sorted(kinds) for ext in EXTENSIONS_BY_KIND[kind]]
    return [extension_pattern(extension) for extension in extensions]


def foreign_tracker_domains(trackers: TrackerDomains, target_host: str) -> tuple[str, ...]:
    """The tracker domains that ``target_host`` does not belong to.

    Readiness ignores requests to these domains. When the page is the tracker vendor's own
    site, its assets live on the listed domain and must count for readiness.
    """
    host = target_host.lower()
    return tuple(
        domain for domain in trackers if host != domain and not host.endswith(f".{domain}")
    )


def describe(kinds: frozenset[BlockedResource]) -> str:
    """The blocked kinds for the timing log, e.g. ``font,image``, or ``none``."""
    return ",".join(sorted(kinds)) or NO_BLOCKING


def load_tracker_domains(path: Path) -> TrackerDomains:
    """Read the tracker domain list: one domain per line, ``#`` comments, blank lines.

    :raises ConfigError: if the file is missing, unreadable, empty or has a malformed line,
        so that readiness can never silently stop ignoring trackers.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"Tracker domain list {path} cannot be read: {exc}") from exc
    domains = tuple(_domains_in(lines, path))
    if not domains:
        raise ConfigError(f"Tracker domain list {path} contains no domains")
    return domains


def _domains_in(lines: list[str], path: Path) -> Iterable[str]:
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith(COMMENT_PREFIX):
            continue
        if not DOMAIN_SYNTAX.match(line):
            raise ConfigError(f"Tracker domain list {path}: line {number} is not a domain")
        yield line


@cache
def default_tracker_domains() -> TrackerDomains:
    """The shipped tracker list, read once; called at startup so a broken file fails fast.

    :raises ConfigError: see ``load_tracker_domains``.
    """
    return load_tracker_domains(TRACKER_LIST_PATH)
