import pytest

import app.scraping.profiles as profiles
from app.browser.readiness import MAX_QUIET_SECONDS, QUIET_GAP_FACTOR, ContentTiming
from app.scraping.profiles import (
    MIN_READY_MARGIN_SECONDS,
    PROFILE_OBSERVE_FIRST_RENDERS,
    PROFILE_WINDOW_SAMPLES,
    ProfilePolicy,
    SectionProfileStore,
)
from tests.fakes import ManualClock

TTL_SECONDS = 100.0
OBSERVE_SECONDS = 10.0
SAMPLE_RATE = 0.05
SECTION = "quotes.example/js-delayed/2"
OTHER_SECTION = "quotes.example/js-delayed/3"
QUICK_PAGE = ContentTiming(last_growth_seconds=0.3, largest_gap_seconds=0.2)
LATE_GROWTH_SECONDS = 10.0


class ScriptedChance:
    """Random source that returns fixed values and counts how often it was asked."""

    def __init__(self, value: float):
        self.value = value
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.value


@pytest.fixture
def clock():
    return ManualClock()


def make_store(clock, chance=None, observe_seconds=OBSERVE_SECONDS) -> SectionProfileStore:
    policy = ProfilePolicy(TTL_SECONDS, observe_seconds, SAMPLE_RATE)
    return SectionProfileStore(policy, clock, chance or ScriptedChance(1.0))


def timing(last_growth: float, gap: float = 0.0) -> ContentTiming:
    return ContentTiming(last_growth_seconds=last_growth, largest_gap_seconds=gap)


def observe_completely(store: SectionProfileStore, renders: int) -> None:
    """Record ``renders`` renders of ``SECTION`` whose late observation finished quietly."""
    for _ in range(renders):
        store.record_late(store.record(SECTION, QUICK_PAGE), None)


def record_many(store: SectionProfileStore, timings: list[ContentTiming]) -> None:
    for item in timings:
        store.record(SECTION, item)


def test_unknown_section_has_no_hints(clock):
    store = make_store(clock)

    assert store.hints(SECTION) is None


def test_single_render_gives_its_last_growth_plus_margin(clock):
    store = make_store(clock)

    store.record(SECTION, timing(2.0))

    assert store.hints(SECTION).min_ready_seconds == 2.0 + MIN_READY_MARGIN_SECONDS


def test_min_ready_is_the_90th_percentile_of_last_growth(clock):
    store = make_store(clock)

    record_many(store, [timing(float(seconds)) for seconds in range(1, 11)])

    assert store.hints(SECTION).min_ready_seconds == 9.0 + MIN_READY_MARGIN_SECONDS


def test_quiet_floor_is_the_gap_factor_times_the_90th_percentile_gap(clock):
    store = make_store(clock)

    store.record(SECTION, timing(1.0, gap=0.8))

    assert store.hints(SECTION).quiet_floor_seconds == QUIET_GAP_FACTOR * 0.8


def test_quiet_floor_is_capped_at_the_maximum_quiet_window(clock):
    store = make_store(clock)

    store.record(SECTION, timing(1.0, gap=9.0))

    assert store.hints(SECTION).quiet_floor_seconds == MAX_QUIET_SECONDS


def test_late_content_raises_the_sample_of_its_own_render(clock):
    store = make_store(clock)
    ref = store.record(SECTION, QUICK_PAGE)

    store.record_late(ref, LATE_GROWTH_SECONDS)

    assert store.hints(SECTION).min_ready_seconds == LATE_GROWTH_SECONDS + MIN_READY_MARGIN_SECONDS


def test_late_observation_without_growth_keeps_the_sample(clock):
    store = make_store(clock)
    ref = store.record(SECTION, timing(2.0))

    store.record_late(ref, None)

    assert store.hints(SECTION).min_ready_seconds == 2.0 + MIN_READY_MARGIN_SECONDS


def test_late_content_never_lowers_a_later_growth_of_the_render(clock):
    store = make_store(clock)
    ref = store.record(SECTION, timing(5.0))

    store.record_late(ref, 1.0)

    assert store.hints(SECTION).min_ready_seconds == 5.0 + MIN_READY_MARGIN_SECONDS


def test_late_content_is_kept_when_its_sample_was_rotated_out(clock, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_WINDOW_SAMPLES", 2)
    store = make_store(clock)
    ref = store.record(SECTION, QUICK_PAGE)
    record_many(store, [QUICK_PAGE, QUICK_PAGE])

    store.record_late(ref, LATE_GROWTH_SECONDS)

    assert store.hints(SECTION).min_ready_seconds == LATE_GROWTH_SECONDS + MIN_READY_MARGIN_SECONDS


def test_only_the_most_recent_samples_count(clock):
    store = make_store(clock)
    store.record(SECTION, timing(LATE_GROWTH_SECONDS))

    record_many(store, [QUICK_PAGE] * PROFILE_WINDOW_SAMPLES)

    expected = QUICK_PAGE.last_growth_seconds + MIN_READY_MARGIN_SECONDS
    assert store.hints(SECTION).min_ready_seconds == expected


def test_profile_expires_after_ttl_counted_from_its_first_render(clock):
    store = make_store(clock)
    store.record(SECTION, QUICK_PAGE)
    clock.advance(TTL_SECONDS / 2)
    store.record(SECTION, QUICK_PAGE)

    clock.advance(TTL_SECONDS / 2)

    assert store.hints(SECTION) is None
    assert store.count() == 0


def test_late_report_for_an_expired_profile_is_ignored(clock):
    store = make_store(clock)
    ref = store.record(SECTION, QUICK_PAGE)
    clock.advance(TTL_SECONDS)

    store.record_late(ref, LATE_GROWTH_SECONDS)

    assert store.hints(SECTION) is None


def test_store_keeps_at_most_the_maximum_number_of_sections(clock, monkeypatch):
    monkeypatch.setattr(profiles, "MAX_PROFILED_SECTIONS", 2)
    store = make_store(clock)

    store.record("site0.example//0", QUICK_PAGE)
    store.record("site1.example//0", QUICK_PAGE)
    store.record("site2.example//0", QUICK_PAGE)

    assert store.count() == 2
    assert store.hints("site0.example//0") is None


def test_recently_recorded_section_survives_eviction(clock, monkeypatch):
    monkeypatch.setattr(profiles, "MAX_PROFILED_SECTIONS", 2)
    store = make_store(clock)
    store.record("site0.example//0", QUICK_PAGE)
    store.record("site1.example//0", QUICK_PAGE)
    store.record("site0.example//0", QUICK_PAGE)

    store.record("site2.example//0", QUICK_PAGE)

    assert store.hints("site0.example//0") is not None
    assert store.hints("site1.example//0") is None


def test_first_renders_of_a_section_are_always_observed(clock):
    chance = ScriptedChance(1.0)
    store = make_store(clock, chance)
    observe_completely(store, PROFILE_OBSERVE_FIRST_RENDERS - 1)

    seconds = store.observe_seconds_for(SECTION)

    assert seconds == OBSERVE_SECONDS
    assert chance.calls == 0


def test_later_renders_are_observed_only_when_sampled(clock):
    store = make_store(clock, ScriptedChance(SAMPLE_RATE))
    observe_completely(store, PROFILE_OBSERVE_FIRST_RENDERS)

    assert store.observe_seconds_for(SECTION) == 0.0


def test_later_renders_are_observed_when_the_sample_hits(clock):
    store = make_store(clock, ScriptedChance(SAMPLE_RATE / 2))
    observe_completely(store, PROFILE_OBSERVE_FIRST_RENDERS)

    assert store.observe_seconds_for(SECTION) == OBSERVE_SECONDS


def test_unfinished_observations_do_not_count_as_observed(clock):
    store = make_store(clock, ScriptedChance(1.0))

    record_many(store, [QUICK_PAGE] * (PROFILE_OBSERVE_FIRST_RENDERS + 1))

    assert store.observe_seconds_for(SECTION) == OBSERVE_SECONDS


def test_observation_disabled_by_zero_seconds(clock):
    store = make_store(clock, observe_seconds=0.0)

    assert store.observe_seconds_for(OTHER_SECTION) == 0.0


def test_unverified_section_is_observed_after_its_first_renders_even_when_not_sampled(clock):
    store = make_store(clock, ScriptedChance(1.0))
    observe_completely(store, PROFILE_OBSERVE_FIRST_RENDERS)

    assert store.observe_seconds_for(SECTION, unverified=True) == OBSERVE_SECONDS


def test_unverified_section_is_not_observed_when_observation_is_disabled(clock):
    store = make_store(clock, observe_seconds=0.0)

    assert store.observe_seconds_for(SECTION, unverified=True) == 0.0
