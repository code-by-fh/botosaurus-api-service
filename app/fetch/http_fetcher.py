"""Browserless page fetch with a real Chrome TLS/HTTP2 fingerprint (curl_cffi).

Used as the fast path in front of the browser. Redirects are followed manually
so that every hop passes the SSRF guard, and response bodies are size-capped.
Cookies are never persisted, so client apps cannot see each other's sessions.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import UnicodeDammit
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from app.errors import NavigationError
from app.url_guard import UrlGuard

log = logging.getLogger("render.http")

IMPERSONATE_TARGET = "chrome"
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


@dataclass(frozen=True)
class HttpPage:
    """Outcome of a browserless fetch."""

    status: int
    content_type: str
    html: str
    final_url: str
    truncated: bool

    @property
    def is_html_document(self) -> bool:
        """True for a complete ``200`` HTML response that was not cut off."""
        is_html = self.content_type.lower().startswith(HTML_CONTENT_TYPES)
        return self.status == 200 and is_html and not self.truncated


@dataclass(frozen=True)
class HttpFetchRequest:
    """Parameters of a single browserless fetch."""

    url: str
    timeout_seconds: float
    proxy_url: str | None


def accept_language_for(locale: str) -> str:
    """Build the ``Accept-Language`` header Chrome sends for ``--lang=<locale>``."""
    language = locale.split("-")[0]
    if language == locale:
        return locale
    return f"{locale},{language};q=0.9"


class HttpFetcher:
    """Fetches documents over HTTP while impersonating Chrome."""

    def __init__(self, guard: UrlGuard, locale: str, max_bytes: int):
        self._guard = guard
        self._max_bytes = max_bytes
        self._headers = {"Accept-Language": accept_language_for(locale)}
        self._session = AsyncSession(impersonate=IMPERSONATE_TARGET, discard_cookies=True)

    async def close(self) -> None:
        """Release pooled connections."""
        await self._session.close()

    async def fetch(self, request: HttpFetchRequest) -> HttpPage:
        """Fetch ``request.url``, following at most ``MAX_REDIRECTS`` guarded redirects.

        :raises NavigationError: on network errors or too many redirects.
        :raises TargetNotAllowedError: if a redirect points to a forbidden host.
        """
        url = request.url
        for _ in range(MAX_REDIRECTS + 1):
            await self._guard.check(url)
            page, location = await self._fetch_once(url, request)
            if location is None:
                return page
            url = urljoin(url, location)
        raise NavigationError(f"Too many redirects for {request.url}")

    async def _fetch_once(self, url: str, request: HttpFetchRequest) -> tuple[HttpPage, str | None]:
        try:
            async with self._session.stream(
                "GET",
                url,
                headers=self._headers,
                timeout=request.timeout_seconds,
                proxy=request.proxy_url,
                allow_redirects=False,
            ) as response:
                return await self._read(url, response)
        except RequestException as exc:
            # The curl message can contain proxy addresses; it is logged, not returned.
            log.info("HTTP fetch failed for %s: %s", url, exc)
            raise NavigationError(f"HTTP fetch failed for {url}") from exc

    async def _read(self, url: str, response) -> tuple[HttpPage, str | None]:
        location = response.headers.get("location")
        if response.status_code in REDIRECT_STATUSES and location:
            return HttpPage(response.status_code, "", "", url, False), location
        body, truncated = await self._read_capped(response)
        page = HttpPage(
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            html=UnicodeDammit(body, [response.charset]).unicode_markup or "",
            final_url=url,
            truncated=truncated,
        )
        return page, None

    async def _read_capped(self, response) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_content():
            size += len(chunk)
            if size > self._max_bytes:
                log.info("Response exceeded %d bytes, stopped reading", self._max_bytes)
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False
