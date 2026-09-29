"""Server-side request forgery (SSRF) protection for target URLs.

The service fetches arbitrary URLs on behalf of client apps. Without this guard
a caller could reach the VPS itself, the cloud metadata endpoint
(169.254.169.254) or, through ``HOME_PROXY``, the private home network.
"""

import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from app.errors import NavigationError, TargetNotAllowedError

ALLOWED_SCHEMES = frozenset({"http", "https"})
DNS_CACHE_SECONDS = 60.0
DNS_CACHE_ENTRIES = 2048
DNS_THREADS = 4

Resolver = Callable[[str], Awaitable[list[str]]]


class CachingResolver:
    """System resolver with a short cache and its own threads.

    Every connection Chrome opens is vetted, so lookups are frequent. The
    dedicated threads keep them from queueing behind HTML parsing, which runs
    in the default executor.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=DNS_THREADS, thread_name_prefix="dns")
        self._cache: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()

    async def __call__(self, host: str) -> list[str]:
        cached = self._cache.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            self._executor, lambda: socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        )
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        self._remember(host, addresses)
        return addresses

    def _remember(self, host: str, addresses: list[str]) -> None:
        self._cache[host] = (time.monotonic() + DNS_CACHE_SECONDS, addresses)
        self._cache.move_to_end(host)
        while len(self._cache) > DNS_CACHE_ENTRIES:
            self._cache.popitem(last=False)


resolve_host = CachingResolver()


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class UrlGuard:
    """Rejects URLs whose scheme is not http(s) or whose host is not public."""

    def __init__(self, allow_private: bool, resolver: Resolver = resolve_host):
        self._allow_private = allow_private
        self._resolver = resolver

    async def check(self, url: str) -> None:
        """Validate ``url`` before any request is sent to it.

        :raises TargetNotAllowedError: for a disallowed scheme or a host that
            resolves to a non-public address.
        :raises NavigationError: if the host cannot be resolved.
        """
        parts = urlsplit(url)
        if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
            raise TargetNotAllowedError("Only absolute http(s) URLs are allowed")
        await self.vetted_addresses(parts.hostname)

    async def vetted_addresses(self, host: str) -> list[str]:
        """Resolve ``host`` once and return the addresses that are safe to connect to.

        Connecting to exactly these addresses (instead of resolving again)
        prevents DNS rebinding between the check and the connection. With
        private targets allowed, ``[host]`` is returned unresolved.

        :raises TargetNotAllowedError: if any address of ``host`` is not public.
        :raises NavigationError: if ``host`` cannot be resolved.
        """
        if self._allow_private:
            return [host]
        addresses = await self._resolve(host)
        if not all(_is_public(address) for address in addresses):
            raise TargetNotAllowedError("The target host resolves to a private or reserved address")
        return addresses

    async def _resolve(self, host: str) -> list[str]:
        try:
            addresses = await self._resolver(host)
        except (OSError, UnicodeError) as exc:
            raise NavigationError(f"The target host could not be resolved: {host}") from exc
        if not addresses:
            raise NavigationError(f"The target host could not be resolved: {host}")
        return addresses
