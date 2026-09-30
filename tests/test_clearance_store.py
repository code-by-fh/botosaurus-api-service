import pytest

from app.scraping.clearance import (
    ClearanceCookie,
    ClearanceStore,
    EgressRoute,
    domain_matches,
    is_clearance_cookie,
    route_for,
)

NOW = 1_000_000.0
MAX_AGE_SECONDS = 240.0
HOST = "shop.example.com"


class FakeClock:
    """Wall clock that only moves when the test says so."""

    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


def clearance_cookie(name: str = "aws-waf-token", **changes) -> ClearanceCookie:
    values = {
        "name": name,
        "value": "token-value",
        "domain": ".example.com",
        "path": "/",
        "secure": True,
        "http_only": False,
        "same_site": "Lax",
        "expires": NOW + 3600,
    }
    values.update(changes)
    return ClearanceCookie(**values)


def new_store(clock: FakeClock | None = None, capacity: int = 16) -> ClearanceStore:
    return ClearanceStore(MAX_AGE_SECONDS, clock or FakeClock(), capacity)


@pytest.mark.parametrize(
    "name",
    [
        "aws-waf-token",
        "cf_clearance",
        "datadome",
        "reese84",
        "incap_ses_1234_567",
        "visid_incap_567",
        "_px3",
        "_pxvid",
        "_abck",
        "bm_sz",
    ],
)
def test_vendor_clearance_cookies_are_allowed(name):
    assert is_clearance_cookie(name)


@pytest.mark.parametrize(
    "name",
    ["session", "PHPSESSID", "_ga", "consent", "cf_clearance_extra", "incap", "AWS-WAF-TOKEN"],
)
def test_other_cookies_are_not_clearance_cookies(name):
    assert not is_clearance_cookie(name)


@pytest.mark.parametrize(
    "cookie_domain, host",
    [
        (".example.com", "example.com"),
        (".example.com", "shop.example.com"),
        (".example.com", "a.b.example.com"),
        ("example.com", "example.com"),
        (".Example.COM", "SHOP.example.com"),
    ],
)
def test_domain_matches_host_and_subdomains(cookie_domain, host):
    assert domain_matches(cookie_domain, host)


@pytest.mark.parametrize(
    "cookie_domain, host",
    [
        (".example.com", "evil-example.com"),
        (".example.com", "example.com.evil.test"),
        (".shop.example.com", "example.com"),
        ("example.com", "shop.example.com"),
        (".example.com", ""),
    ],
)
def test_domain_does_not_match_lookalikes_or_parents(cookie_domain, host):
    assert not domain_matches(cookie_domain, host)


def test_route_follows_the_proxy_choice():
    assert route_for(use_proxy=False) is EgressRoute.DIRECT
    assert route_for(use_proxy=True) is EgressRoute.HOME_PROXY


def test_stored_clearance_cookie_is_returned_for_matching_host():
    store = new_store()

    stored = store.store(EgressRoute.DIRECT, [clearance_cookie()])

    assert stored == 1
    assert store.matching(EgressRoute.DIRECT, HOST) == [clearance_cookie()]


def test_only_allow_listed_cookies_are_stored():
    store = new_store()

    stored = store.store(
        EgressRoute.DIRECT, [clearance_cookie("session_id"), clearance_cookie("cf_clearance")]
    )

    assert stored == 1
    assert [cookie.name for cookie in store.matching(EgressRoute.DIRECT, HOST)] == ["cf_clearance"]


def test_cookie_from_one_route_is_never_used_on_the_other():
    store = new_store()

    store.store(EgressRoute.DIRECT, [clearance_cookie()])

    assert store.matching(EgressRoute.HOME_PROXY, HOST) == []


def test_cookie_is_not_returned_for_a_lookalike_host():
    store = new_store()

    store.store(EgressRoute.DIRECT, [clearance_cookie()])

    assert store.matching(EgressRoute.DIRECT, "evil-example.com") == []


def test_cookie_expires_at_its_own_expiry_before_max_age():
    clock = FakeClock()
    store = new_store(clock)
    store.store(EgressRoute.DIRECT, [clearance_cookie(expires=NOW + 60)])

    clock.now = NOW + 60

    assert store.matching(EgressRoute.DIRECT, HOST) == []


def test_cookie_expires_at_max_age_before_its_own_expiry():
    clock = FakeClock()
    store = new_store(clock)
    store.store(EgressRoute.DIRECT, [clearance_cookie()])

    clock.now = NOW + MAX_AGE_SECONDS - 1
    still_valid = store.matching(EgressRoute.DIRECT, HOST)
    clock.now = NOW + MAX_AGE_SECONDS

    assert len(still_valid) == 1
    assert store.matching(EgressRoute.DIRECT, HOST) == []


def test_session_cookie_lives_for_max_age():
    clock = FakeClock()
    store = new_store(clock)
    store.store(EgressRoute.DIRECT, [clearance_cookie("incap_ses_1_2", expires=None)])

    clock.now = NOW + MAX_AGE_SECONDS

    assert store.matching(EgressRoute.DIRECT, HOST) == []


def test_already_expired_cookie_is_not_stored():
    store = new_store()

    stored = store.store(EgressRoute.DIRECT, [clearance_cookie(expires=NOW - 1)])

    assert stored == 0
    assert store.count() == 0


def test_newer_cookie_with_same_name_replaces_the_older_one():
    store = new_store()
    store.store(EgressRoute.DIRECT, [clearance_cookie(value="old")])

    store.store(EgressRoute.DIRECT, [clearance_cookie(value="new")])

    assert [cookie.value for cookie in store.matching(EgressRoute.DIRECT, HOST)] == ["new"]


def test_oldest_entry_is_evicted_at_capacity():
    store = new_store(capacity=2)
    store.store(EgressRoute.DIRECT, [clearance_cookie(domain=".first.example")])
    store.store(EgressRoute.DIRECT, [clearance_cookie(domain=".second.example")])

    store.store(EgressRoute.DIRECT, [clearance_cookie(domain=".third.example")])

    assert store.count() == 2
    assert store.matching(EgressRoute.DIRECT, "first.example") == []
    assert len(store.matching(EgressRoute.DIRECT, "third.example")) == 1


def test_forget_drops_only_the_matching_entries_of_that_route():
    store = new_store()
    store.store(EgressRoute.DIRECT, [clearance_cookie(), clearance_cookie(domain=".other.test")])
    store.store(EgressRoute.HOME_PROXY, [clearance_cookie()])

    dropped = store.forget(EgressRoute.DIRECT, HOST)

    assert dropped == 1
    assert store.matching(EgressRoute.DIRECT, HOST) == []
    assert len(store.matching(EgressRoute.DIRECT, "other.test")) == 1
    assert len(store.matching(EgressRoute.HOME_PROXY, HOST)) == 1


def test_count_ignores_expired_entries():
    clock = FakeClock()
    store = new_store(clock)
    store.store(EgressRoute.DIRECT, [clearance_cookie()])

    clock.now = NOW + MAX_AGE_SECONDS

    assert store.count() == 0


def test_cookie_value_is_not_part_of_its_representation():
    assert "token-value" not in repr(clearance_cookie())
