import pytest

from app.scraping.verdicts import (
    MAX_MATCHES_PER_SECTION,
    Verdict,
    VerdictPolicy,
    VerdictStore,
    section_key,
)

TTL_SECONDS = 10_000.0
SAME_PAGE_INTERVAL_SECONDS = 600.0
SECTION = "shop.example/products/2"
FIRST_URL = "https://shop.example/products/1"
SECOND_URL = "https://shop.example/products/2"
FIRST_TEXT = "Product one costs 10 EUR"
SECOND_TEXT = "Product two costs 20 EUR"


class ManualClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def store(clock):
    policy = VerdictPolicy(
        TTL_SECONDS, min_samples=2, same_page_interval_seconds=SAME_PAGE_INTERVAL_SECONDS
    )
    return VerdictStore(policy, clock)


def text_for(url: str) -> str:
    """A distinct browser text per page, so only the URL decides the identity."""
    return f"Content of {url}"


def record_distinct_pages(store: VerdictStore, count: int) -> None:
    for number in range(count):
        record(store, f"https://shop.example/products/{number}")


def record(store: VerdictStore, url: str, text: str | None = None) -> None:
    store.record_match(SECTION, url, text or text_for(url))


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://Shop.Example/products/42?color=red", "shop.example/products/2"),
        ("https://shop.example/products", "shop.example/products/1"),
        ("https://shop.example/", "shop.example//0"),
        ("https://shop.example", "shop.example//0"),
    ],
)
def test_section_key_is_host_first_segment_and_depth(url, expected):
    assert section_key(url) == expected


def test_unknown_section_has_unknown_verdict(store):
    assert store.get(SECTION) is Verdict.UNKNOWN


def test_one_match_is_not_enough_evidence(store):
    record(store, FIRST_URL)

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_repeated_matches_of_one_url_count_once(store):
    record(store, FIRST_URL)
    record(store, FIRST_URL)

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_query_variants_of_one_page_count_once(store):
    record(store, FIRST_URL + "?utm_source=a")
    record(store, FIRST_URL + "?utm_source=b#top")

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_matches_of_different_urls_make_http_sufficient(store):
    record(store, FIRST_URL)
    record(store, SECOND_URL)

    assert store.get(SECTION) is Verdict.HTTP_SUFFICIENT


def test_mismatch_makes_section_browser_only(store):
    record(store, FIRST_URL)
    record(store, SECOND_URL)

    store.record_mismatch(SECTION)

    assert store.get(SECTION) is Verdict.BROWSER_REQUIRED


def test_matches_do_not_override_browser_required(store):
    store.record_mismatch(SECTION)

    record(store, FIRST_URL)
    record(store, SECOND_URL)

    assert store.get(SECTION) is Verdict.BROWSER_REQUIRED


def test_verdict_expires_after_ttl(store, clock):
    store.record_mismatch(SECTION)

    clock.now += TTL_SECONDS

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_counts_report_live_sections_per_verdict(store):
    store.record_mismatch("a.example/")
    store.record_match("b.example/", "https://b.example/one", FIRST_TEXT)
    store.record_match("b.example/", "https://b.example/two", SECOND_TEXT)

    assert store.counts() == {"unknown": 0, "http_sufficient": 1, "browser_required": 1}


@pytest.mark.parametrize(
    "variant",
    [
        "https://shop.example/products/A",
        "https://shop.example/products/%61",
        "https://shop.example//products/a",
        "https://shop.example/products/a;jsessionid=X",
        "https://shop.example/products/a/",
    ],
)
def test_spelling_variants_of_one_page_count_once(store, variant):
    record(store, "https://shop.example/products/a")
    record(store, variant)

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_encoded_reserved_characters_still_tell_pages_apart(store):
    record(store, "https://shop.example/products/a%2Fb")
    record(store, "https://shop.example/products/a")

    assert store.get(SECTION) is Verdict.HTTP_SUFFICIENT


def test_different_urls_showing_the_same_text_count_once(store):
    record(store, FIRST_URL, FIRST_TEXT)
    record(store, SECOND_URL, "  product ONE costs 10 eur ")

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_a_match_sharing_url_with_one_page_and_text_with_another_joins_them(clock):
    third_url = "https://shop.example/products/3"
    store = VerdictStore(VerdictPolicy(TTL_SECONDS, 3, SAME_PAGE_INTERVAL_SECONDS), clock)
    record(store, FIRST_URL, FIRST_TEXT)
    record(store, third_url, SECOND_TEXT)
    record(store, FIRST_URL, SECOND_TEXT)

    record(store, SECOND_URL, "Product three costs 30 EUR")

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_same_page_matching_again_after_the_interval_makes_http_sufficient(store, clock):
    record(store, FIRST_URL)
    clock.now += SAME_PAGE_INTERVAL_SECONDS

    record(store, FIRST_URL)

    assert store.get(SECTION) is Verdict.HTTP_SUFFICIENT


def test_same_page_matching_again_within_the_interval_is_not_enough(store, clock):
    record(store, FIRST_URL)
    clock.now += SAME_PAGE_INTERVAL_SECONDS - 1

    record(store, FIRST_URL)

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_mismatch_after_same_page_matches_makes_section_browser_only(store, clock):
    record(store, FIRST_URL)
    clock.now += SAME_PAGE_INTERVAL_SECONDS
    record(store, FIRST_URL)

    store.record_mismatch(SECTION)

    assert store.get(SECTION) is Verdict.BROWSER_REQUIRED


def test_unknown_section_wants_a_sample(store):
    assert store.wants_sample(SECTION, FIRST_URL) is True


def test_page_matched_within_the_interval_wants_no_new_sample(store, clock):
    record(store, FIRST_URL)
    clock.now += SAME_PAGE_INTERVAL_SECONDS - 1

    assert store.wants_sample(SECTION, FIRST_URL + "/") is False
    assert store.wants_sample(SECTION, SECOND_URL) is True


def test_page_matched_before_the_interval_wants_a_new_sample(store, clock):
    record(store, FIRST_URL)
    clock.now += SAME_PAGE_INTERVAL_SECONDS

    assert store.wants_sample(SECTION, FIRST_URL) is True


def test_section_with_a_verdict_wants_no_sample(store):
    store.record_mismatch(SECTION)

    assert store.wants_sample(SECTION, FIRST_URL) is False


def test_matches_beyond_the_per_section_limit_are_ignored(clock):
    policy = VerdictPolicy(TTL_SECONDS, MAX_MATCHES_PER_SECTION + 1, SAME_PAGE_INTERVAL_SECONDS)
    store = VerdictStore(policy, clock)
    record_distinct_pages(store, MAX_MATCHES_PER_SECTION + 1)

    assert store.get(SECTION) is Verdict.UNKNOWN
