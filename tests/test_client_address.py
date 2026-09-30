import ipaddress

from starlette.requests import Request

from app.client_address import client_address

PROXY = "10.0.0.2"
CLIENT = "203.0.113.7"
TRUSTED = (ipaddress.ip_network("10.0.0.0/24"),)


def request_from(peer: str, forwarded_for: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded_for.encode())] if forwarded_for else []
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


def test_direct_peer_is_used_without_trusted_proxies():
    request = request_from(CLIENT, forwarded_for="198.51.100.1")

    assert client_address(request, ()) == CLIENT


def test_forwarded_for_from_an_untrusted_peer_is_ignored():
    request = request_from(CLIENT, forwarded_for="198.51.100.1")

    assert client_address(request, TRUSTED) == CLIENT


def test_trusted_proxy_reveals_the_forwarded_client():
    request = request_from(PROXY, forwarded_for=CLIENT)

    assert client_address(request, TRUSTED) == CLIENT


def test_spoofed_leftmost_hops_are_skipped():
    request = request_from(PROXY, forwarded_for=f"198.51.100.1, {CLIENT}")

    assert client_address(request, TRUSTED) == CLIENT


def test_chained_trusted_proxies_are_walked_from_the_right():
    request = request_from(PROXY, forwarded_for=f"{CLIENT}, 10.0.0.9")

    assert client_address(request, TRUSTED) == CLIENT


def test_trusted_proxy_without_forwarded_for_is_the_client():
    request = request_from(PROXY)

    assert client_address(request, TRUSTED) == PROXY
