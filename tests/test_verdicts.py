import pytest

from app.scraping.verdicts import Verdict, VerdictStore, section_key

TTL_SECONDS = 100.0
SECTION = "shop.example/products/2"
FIRST_URL = "https://shop.example/products/1"
SECOND_URL = "https://shop.example/products/2"


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
    return VerdictStore(ttl_seconds=TTL_SECONDS, min_samples=2, clock=clock)


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
    store.record_match(SECTION, FIRST_URL)

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_repeated_matches_of_one_url_count_once(store):
    store.record_match(SECTION, FIRST_URL)
    store.record_match(SECTION, FIRST_URL)

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_query_variants_of_one_page_count_once(store):
    store.record_match(SECTION, FIRST_URL + "?utm_source=a")
    store.record_match(SECTION, FIRST_URL + "?utm_source=b#top")

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_matches_of_different_urls_make_http_sufficient(store):
    store.record_match(SECTION, FIRST_URL)
    store.record_match(SECTION, SECOND_URL)

    assert store.get(SECTION) is Verdict.HTTP_SUFFICIENT


def test_mismatch_makes_section_browser_only(store):
    store.record_match(SECTION, FIRST_URL)
    store.record_match(SECTION, SECOND_URL)

    store.record_mismatch(SECTION)

    assert store.get(SECTION) is Verdict.BROWSER_REQUIRED


def test_matches_do_not_override_browser_required(store):
    store.record_mismatch(SECTION)

    store.record_match(SECTION, FIRST_URL)
    store.record_match(SECTION, SECOND_URL)

    assert store.get(SECTION) is Verdict.BROWSER_REQUIRED


def test_verdict_expires_after_ttl(store, clock):
    store.record_mismatch(SECTION)

    clock.now += TTL_SECONDS

    assert store.get(SECTION) is Verdict.UNKNOWN


def test_counts_report_live_sections_per_verdict(store):
    store.record_mismatch("a.example/")
    store.record_match("b.example/", "https://b.example/one")
    store.record_match("b.example/", "https://b.example/two")

    assert store.counts() == {"unknown": 0, "http_sufficient": 1, "browser_required": 1}
