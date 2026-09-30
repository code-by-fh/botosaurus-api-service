"""Determines the address of the client that sent a request.

The direct peer is used unless it is a proxy listed in ``TRUSTED_PROXY_IPS``.
Only then is ``X-Forwarded-For`` consulted, because any client can send that
header and would otherwise pick its own address (and dodge the auth throttle).
"""

import ipaddress

from starlette.requests import HTTPConnection

from app.config import IpNetwork

FORWARDED_FOR_HEADER = "x-forwarded-for"
UNKNOWN_CLIENT = "unknown"


def _is_trusted(address: str, trusted: tuple[IpNetwork, ...]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in trusted)


def _forwarded_hops(connection: HTTPConnection) -> list[str]:
    values = connection.headers.getlist(FORWARDED_FOR_HEADER)
    return [hop.strip() for value in values for hop in value.split(",") if hop.strip()]


def client_address(connection: HTTPConnection, trusted: tuple[IpNetwork, ...]) -> str:
    """Return the client address, honouring ``X-Forwarded-For`` only behind trusted proxies.

    The forwarded chain is read from the right, because each proxy appends the
    address it saw; the first hop that is not a trusted proxy is the client.
    """
    peer = connection.client.host if connection.client else UNKNOWN_CLIENT
    if not _is_trusted(peer, trusted):
        return peer
    for hop in reversed(_forwarded_hops(connection)):
        if not _is_trusted(hop, trusted):
            return hop
    return peer
