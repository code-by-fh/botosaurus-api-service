import pytest

from app.errors import NavigationError, TargetNotAllowedError
from app.url_guard import UrlGuard


def resolver_returning(*addresses: str):
    async def resolve(host: str) -> list[str]:
        return list(addresses)

    return resolve


async def failing_resolver(host: str) -> list[str]:
    raise OSError("Name or service not known")


@pytest.mark.anyio
async def test_public_address_is_allowed():
    guard = UrlGuard(allow_private=False, resolver=resolver_returning("93.184.215.14"))

    result = await guard.check("https://example.com/page")

    assert result is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "fd00::1",
    ],
)
async def test_non_public_addresses_are_rejected(address):
    guard = UrlGuard(allow_private=False, resolver=resolver_returning(address))

    with pytest.raises(TargetNotAllowedError, match="private or reserved"):
        await guard.check("http://internal.example/")


@pytest.mark.anyio
async def test_host_with_any_private_address_is_rejected():
    guard = UrlGuard(allow_private=False, resolver=resolver_returning("93.184.215.14", "10.0.0.1"))

    with pytest.raises(TargetNotAllowedError):
        await guard.check("http://mixed.example/")


@pytest.mark.anyio
@pytest.mark.parametrize("url", ["ftp://example.com/file", "file:///etc/passwd", "http:///nohost"])
async def test_non_http_urls_are_rejected(url):
    guard = UrlGuard(allow_private=True)

    with pytest.raises(TargetNotAllowedError, match="http"):
        await guard.check(url)


@pytest.mark.anyio
async def test_unresolvable_host_is_a_navigation_error():
    guard = UrlGuard(allow_private=False, resolver=failing_resolver)

    with pytest.raises(NavigationError, match="could not be resolved"):
        await guard.check("https://does-not-exist.invalid/")


@pytest.mark.anyio
async def test_private_targets_pass_when_explicitly_allowed():
    guard = UrlGuard(allow_private=True, resolver=failing_resolver)

    result = await guard.check("http://localhost:8080/")

    assert result is None
