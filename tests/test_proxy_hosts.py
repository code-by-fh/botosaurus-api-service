from app.scraping.proxy_hosts import ProxyHostStore
from tests.fakes import ManualClock

TTL_SECONDS = 600.0
HOST = "shop.example"
OTHER_HOST = "news.example"


def store(clock: ManualClock, capacity: int = 10) -> ProxyHostStore:
    return ProxyHostStore(TTL_SECONDS, clock, capacity)


def test_unknown_host_does_not_require_the_proxy():
    hosts = store(ManualClock())

    assert hosts.requires_proxy(HOST) is False


def test_remembered_host_requires_the_proxy_until_the_ttl_expires():
    clock = ManualClock()
    hosts = store(clock)
    hosts.remember(HOST)

    clock.advance(TTL_SECONDS - 1)
    still_remembered = hosts.requires_proxy(HOST)
    clock.advance(1)

    assert still_remembered is True
    assert hosts.requires_proxy(HOST) is False
    assert hosts.count() == 0


def test_host_names_are_normalised():
    hosts = store(ManualClock())

    hosts.remember("Shop.Example.")

    assert hosts.requires_proxy(HOST) is True


def test_remembering_again_restarts_the_ttl():
    clock = ManualClock()
    hosts = store(clock)
    hosts.remember(HOST)
    clock.advance(TTL_SECONDS - 1)

    hosts.remember(HOST)
    clock.advance(TTL_SECONDS - 1)

    assert hosts.requires_proxy(HOST) is True


def test_oldest_host_is_dropped_beyond_the_capacity():
    hosts = store(ManualClock(), capacity=1)
    hosts.remember(OTHER_HOST)

    hosts.remember(HOST)

    assert hosts.requires_proxy(OTHER_HOST) is False
    assert hosts.requires_proxy(HOST) is True
    assert hosts.count() == 1


def test_count_ignores_expired_hosts():
    clock = ManualClock()
    hosts = store(clock)
    hosts.remember(OTHER_HOST)
    clock.advance(TTL_SECONDS)
    hosts.remember(HOST)

    assert hosts.count() == 1
