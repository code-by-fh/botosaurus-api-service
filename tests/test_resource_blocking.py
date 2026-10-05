from pathlib import Path

import pytest

import app.browser.blocking as blocking
from app.browser.blocking import (
    EXTENSIONS_BY_KIND,
    NEVER_BLOCKED_DOMAINS,
    NEVER_BLOCKED_PATTERNS,
    BlockedResource,
    block_patterns,
    default_tracker_domains,
    describe,
    domain_patterns,
    extension_pattern,
    foreign_tracker_domains,
    load_tracker_domains,
)
from app.config import ConfigError
from app.runtime import Adapters, start_runtime
from scripts.update_tracker_domains import MAX_DOMAINS, NEVER_LISTED_DOMAINS, is_covered
from tests.fakes import FakeLauncher, make_settings, public_resolver

FILE_KINDS = (
    BlockedResource.IMAGE,
    BlockedResource.FONT,
    BlockedResource.MEDIA,
    BlockedResource.STYLESHEET,
)
# The extensions the previous substring patterns covered; none may get lost.
PREVIOUSLY_BLOCKED_EXTENSIONS = (
    "png jpg jpeg gif webp avif svg ico woff woff2 ttf otf mp4 webm mp3 m4a css".split()
)
TARGET_HOST = "www.example.com"
TRACKERS = ("tracker.example", "ads.example")


def patterns_of(kinds) -> list[tuple[str, bool]]:
    return [(item.url_pattern, item.block) for item in block_patterns(kinds)]


def blocked_patterns_of(kinds) -> list[str]:
    return [pattern for pattern, block in patterns_of(kinds) if block]


def write_list(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "trackers.txt"
    path.write_text(content, encoding="utf-8")
    return path


def all_extension_patterns() -> list[str]:
    return [extension_pattern(ext) for exts in EXTENSIONS_BY_KIND.values() for ext in exts]


@pytest.mark.parametrize("extension", PREVIOUSLY_BLOCKED_EXTENSIONS)
def test_every_previously_blocked_extension_is_still_covered(extension):
    covered = {ext for exts in EXTENSIONS_BY_KIND.values() for ext in exts}

    assert extension in covered


def test_extension_pattern_matches_any_host_and_port_and_ends_at_the_path():
    assert extension_pattern("css") == "*://*:*/*.css?*"


@pytest.mark.parametrize("pattern", all_extension_patterns())
def test_extension_patterns_are_anchored_urlpatterns_not_substrings(pattern):
    # A bare "*.ico" was a substring match in the deprecated `urls` parameter and blocked
    # e.g. scripts of shop.icon.de; every pattern must pin scheme, host, port and path end.
    assert pattern.startswith("*://*:*/*.")
    assert pattern.endswith("?*")
    assert not pattern.startswith("*.")


def test_domain_patterns_cover_the_domain_and_its_subdomains_on_any_port():
    assert domain_patterns("tracker.example") == (
        "*://tracker.example:*/*",
        "*://*.tracker.example:*/*",
    )


def test_no_blocking_requested_sends_no_patterns():
    assert patterns_of(frozenset()) == []


def test_allow_list_comes_first_and_never_blocks():
    patterns = patterns_of(frozenset({BlockedResource.IMAGE}))

    allowed = patterns[: len(NEVER_BLOCKED_PATTERNS)]
    assert allowed == [(pattern, False) for pattern in NEVER_BLOCKED_PATTERNS]
    assert all(block for _, block in patterns[len(NEVER_BLOCKED_PATTERNS) :])


def test_allow_list_keeps_challenge_vendors_and_recaptcha_reachable():
    assert "*://challenges.cloudflare.com:*/*" in NEVER_BLOCKED_PATTERNS
    assert "*://*.datadome.co:*/*" in NEVER_BLOCKED_PATTERNS
    assert "*://www.google.com:*/recaptcha/*" in NEVER_BLOCKED_PATTERNS
    assert "*://*:*/cdn-cgi/challenge-platform/*" in NEVER_BLOCKED_PATTERNS


def test_image_kind_blocks_only_image_extensions():
    blocked = blocked_patterns_of(frozenset({BlockedResource.IMAGE}))

    assert "*://*:*/*.png?*" in blocked
    assert "*://*:*/*.css?*" not in blocked


def test_blocking_never_covers_tracker_domains():
    # Trackers are ignored by readiness instead (ADR 0005); blocking them is not offered.
    blocked = blocked_patterns_of(frozenset(FILE_KINDS))

    assert blocked == [
        extension_pattern(ext) for kind in sorted(FILE_KINDS) for ext in EXTENSIONS_BY_KIND[kind]
    ]


def test_the_four_file_kinds_are_the_only_blockable_kinds():
    assert frozenset(BlockedResource) == frozenset(FILE_KINDS)


def test_describe_lists_kinds_sorted_or_none():
    kinds = frozenset({BlockedResource.STYLESHEET, BlockedResource.FONT, BlockedResource.IMAGE})

    assert describe(kinds) == "font,image,stylesheet"
    assert describe(frozenset()) == "none"


def test_tracker_list_skips_comments_and_blank_lines(tmp_path):
    path = write_list(tmp_path, "# generated\n\ntracker.example\n  ads.example  \n")

    assert load_tracker_domains(path) == ("tracker.example", "ads.example")


def test_missing_tracker_list_fails_at_startup(tmp_path):
    with pytest.raises(ConfigError, match="Tracker domain list .* cannot be read"):
        load_tracker_domains(tmp_path / "absent.txt")


def test_tracker_list_without_domains_fails_at_startup(tmp_path):
    path = write_list(tmp_path, "# only a header\n")

    with pytest.raises(ConfigError, match="contains no domains"):
        load_tracker_domains(path)


def test_tracker_list_with_a_malformed_line_fails_at_startup(tmp_path):
    path = write_list(tmp_path, "tracker.example\n*://bad/*\n")

    with pytest.raises(ConfigError, match="line 2 is not a domain"):
        load_tracker_domains(path)


def test_shipped_tracker_list_is_bounded_and_not_empty():
    domains = default_tracker_domains()

    assert 0 < len(domains) <= MAX_DOMAINS
    assert len(set(domains)) == len(domains)


def test_shipped_tracker_list_covers_well_known_trackers():
    domains = set(default_tracker_domains())

    assert {"google-analytics.com", "criteo.com", "adnxs.com", "hotjar.com"} <= domains


def test_shipped_tracker_list_never_touches_protected_domains():
    # Anti-bot vendors, tag and consent managers often deliver content or consent state, so
    # readiness must keep waiting for them: neither listed themselves nor hit through a
    # listed parent or subdomain.
    protected = NEVER_BLOCKED_DOMAINS | NEVER_LISTED_DOMAINS
    listed = set(default_tracker_domains())

    assert [domain for domain in protected if is_covered(domain, listed)] == []
    assert [domain for domain in listed if is_covered(domain, protected)] == []


@pytest.fixture
def missing_tracker_list(tmp_path, monkeypatch):
    monkeypatch.setattr(blocking, "TRACKER_LIST_PATH", tmp_path / "absent.txt")
    default_tracker_domains.cache_clear()
    yield
    default_tracker_domains.cache_clear()


@pytest.mark.anyio
async def test_runtime_refuses_to_start_without_tracker_list(missing_tracker_list):
    adapters = Adapters(launcher=FakeLauncher(), resolver=public_resolver)

    with pytest.raises(ConfigError, match="Tracker domain list"):
        await start_runtime(make_settings(), adapters)


@pytest.mark.parametrize("host", ["tracker.example", "www.tracker.example", "TRACKER.example"])
def test_the_tracker_domain_a_page_belongs_to_is_not_foreign(host):
    assert foreign_tracker_domains(TRACKERS, host) == ("ads.example",)


def test_all_tracker_domains_are_foreign_to_an_ordinary_page():
    assert foreign_tracker_domains(TRACKERS, TARGET_HOST) == TRACKERS
