import json
import logging

import pytest
from zendriver.core.connection import ProtocolException

import app.browser.readiness as readiness
from app.browser.activity import ActivityLog
from app.browser.network_activity import LONG_POLL_SECONDS, InflightTracker
from app.browser.readiness import (
    BASE_QUIET_SECONDS,
    LOAD_BUDGET_SHARE,
    LOAD_WATCHDOG_SECONDS,
    LOADING_PLACEHOLDER_NAME_PATTERN,
    LOADING_TEXT_PATTERN,
    MAX_QUIET_SECONDS,
    QUIET_GAP_FACTOR,
    WAIT_FOR_QUIET_CAP_SECONDS,
    ContentTiming,
    ReadinessEnd,
    ReadinessHints,
    ReadinessOptions,
    ReadinessWaiter,
    WaitTarget,
    build_observer_script,
    placeholder_candidate_selector,
)
from app.content.completeness import (
    CHALLENGE_BODY_MARKERS,
    CHALLENGE_SDK_MARKERS,
    CHALLENGE_TITLE_PATTERN,
    HUMAN_VERIFICATION_PATTERN,
    INTERSTITIAL_MAX_TEXT_CHARS,
)
from app.errors import NavigationError, RenderTimeoutError, TargetBlockedError
from tests.fakes import (
    NAVIGATED,
    POLL_STEP_SECONDS,
    ScriptedTab,
    loading_finished,
    request_sent,
    restless_probes,
    stable_probe,
)

# Readiness budgets in seconds of the scripted tab's manual clock.
SHORT_BUDGET_SECONDS = 1.0
LONG_BUDGET_SECONDS = 30.0
QUICK_CAP_SECONDS = 0.25
TRACKERS = frozenset({"google-analytics.com"})
API_URL = "https://shop.example/api/products"
TRACKER_URL = "https://www.google-analytics.com/g/collect"
# Polls until the base quiet window has passed since the last event.
QUIET_POLLS = round(BASE_QUIET_SECONDS / POLL_STEP_SECONDS)
# The readiness check before this change compared four probes 300 ms apart.
OLD_MINIMUM_SECONDS = 1.2


@pytest.fixture(autouse=True)
def production_quiet_window(monkeypatch):
    # conftest zeroes the base window for the integration tests; these tests run on a
    # manual clock and check the real window.
    monkeypatch.setattr(readiness, "BASE_QUIET_SECONDS", BASE_QUIET_SECONDS)


@pytest.fixture
def quick_cap(monkeypatch):
    monkeypatch.setattr(readiness, "WAIT_FOR_QUIET_CAP_SECONDS", QUICK_CAP_SECONDS)


def waiter_for(tab: ScriptedTab, target: WaitTarget) -> ReadinessWaiter:
    activity = ActivityLog(tab.clock)
    tracker = InflightTracker(TRACKERS, activity)
    tracker.attach(tab)
    return ReadinessWaiter(tab, target, ReadinessOptions(activity, tracker))


def elapsed(tab: ScriptedTab) -> float:
    return tab.calls * tab.step


def polls_at(seconds: float) -> int:
    """Number of observations until the manual clock reaches ``seconds``."""
    return round(seconds / POLL_STEP_SECONDS)


# --- returning as early as provable -------------------------------------------------------


@pytest.mark.anyio
async def test_idle_page_returns_once_the_base_quiet_window_has_passed():
    tab = ScriptedTab([stable_probe()])
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is True
    assert waiter.end is ReadinessEnd.SETTLED
    assert elapsed(tab) == BASE_QUIET_SECONDS
    assert elapsed(tab) < OLD_MINIMUM_SECONDS


@pytest.mark.anyio
async def test_page_returns_one_quiet_window_after_its_last_request():
    events = {0: [request_sent("1", API_URL)], 3: [loading_finished("1")]}
    tab = ScriptedTab([stable_probe()], events)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.end is ReadinessEnd.SETTLED
    assert tab.calls == 4 + QUIET_POLLS


@pytest.mark.anyio
async def test_content_request_in_flight_holds_the_page_back_until_it_finishes():
    events = {0: [request_sent("1", API_URL, "Fetch")], 20: [loading_finished("1")]}
    tab = ScriptedTab([stable_probe()], events)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert tab.calls == 21 + QUIET_POLLS
    assert waiter.quiet_seconds == BASE_QUIET_SECONDS


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("url", "resource_type"),
    [(TRACKER_URL, "Script"), (API_URL, "Ping"), (API_URL, "WebSocket"), (API_URL, "Image")],
)
async def test_trackers_beacons_and_websockets_do_not_hold_the_page_back(url, resource_type):
    tab = ScriptedTab([stable_probe()], {0: [request_sent("1", url, resource_type)]})
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert elapsed(tab) == BASE_QUIET_SECONDS
    assert waiter.ignored_requests == 1


@pytest.mark.anyio
async def test_long_poll_stops_holding_the_page_back_after_the_long_poll_limit():
    tab = ScriptedTab([stable_probe()], {0: [request_sent("1", API_URL)]})
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.end is ReadinessEnd.SETTLED
    assert tab.calls == 1 + polls_at(LONG_POLL_SECONDS)
    assert waiter.ignored_requests == 1


# --- DOM growth ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dom_growth_restarts_the_quiet_window():
    growing = [stable_probe(growth=count) for count in (1, 2, 3)]
    tab = ScriptedTab(growing)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is True
    assert tab.calls == len(growing) + QUIET_POLLS


@pytest.mark.anyio
async def test_rotating_known_text_reports_no_growth_and_does_not_hold_the_page_back():
    # The observer script reports the same growth count while a carousel only
    # rotates texts it has seen before; such churn must not keep the page waiting.
    rotating = [stable_probe(growth=4) for _ in range(10)]
    tab = ScriptedTab(rotating)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert elapsed(tab) == BASE_QUIET_SECONDS


@pytest.mark.anyio
async def test_visible_loading_placeholder_holds_the_page_back():
    placeholders = [stable_probe(placeholders=True)] * 8
    tab = ScriptedTab(placeholders + [stable_probe()])
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.end is ReadinessEnd.SETTLED
    assert tab.calls == len(placeholders) + 1


# --- adaptive quiet window ----------------------------------------------------------------


@pytest.mark.anyio
async def test_quiet_window_grows_with_the_idle_gaps_of_a_bursty_page():
    burst_gap_polls = 3
    events = {
        0: [request_sent("1", API_URL)],
        1: [loading_finished("1")],
        1 + burst_gap_polls: [request_sent("2", API_URL)],
        2 + burst_gap_polls: [loading_finished("2")],
    }
    tab = ScriptedTab([stable_probe()], events)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    window = QUIET_GAP_FACTOR * burst_gap_polls * POLL_STEP_SECONDS
    assert window > BASE_QUIET_SECONDS
    assert waiter.quiet_seconds == window
    assert tab.calls == 3 + burst_gap_polls + polls_at(window)


@pytest.mark.anyio
async def test_quiet_window_is_capped():
    loading_polls = 20
    events = {
        0: [request_sent("1", API_URL), loading_finished("1")],
        loading_polls - 1: [request_sent("2", API_URL), loading_finished("2")],
    }
    loading = [stable_probe(ready="loading")] * loading_polls
    tab = ScriptedTab([*loading, stable_probe()], events)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.quiet_seconds == MAX_QUIET_SECONDS
    assert tab.calls == loading_polls + polls_at(MAX_QUIET_SECONDS)


@pytest.mark.anyio
async def test_found_wait_for_element_caps_the_quiet_window(quick_cap):
    tab = ScriptedTab([stable_probe()])
    waiter = waiter_for(tab, WaitTarget("#price", LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is True
    assert waiter.quiet_seconds == QUICK_CAP_SECONDS
    assert elapsed(tab) == QUICK_CAP_SECONDS


def test_wait_for_quiet_cap_is_shorter_than_the_largest_quiet_window():
    assert BASE_QUIET_SECONDS <= WAIT_FOR_QUIET_CAP_SECONDS < MAX_QUIET_SECONDS


# --- learned floors (profiles) ------------------------------------------------------------

LEARNED_MIN_READY_SECONDS = 3.0
LEARNED_QUIET_FLOOR_SECONDS = 2.0


def learned(min_ready: float = 0.0, quiet_floor: float = 0.0) -> ReadinessHints:
    return ReadinessHints(min_ready_seconds=min_ready, quiet_floor_seconds=quiet_floor)


@pytest.mark.anyio
async def test_learned_min_ready_holds_a_quiet_page_back():
    tab = ScriptedTab([stable_probe()])
    hints = learned(min_ready=LEARNED_MIN_READY_SECONDS)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS, hints))

    stable = await waiter.wait()

    assert stable is True
    assert waiter.end is ReadinessEnd.SETTLED
    assert elapsed(tab) == LEARNED_MIN_READY_SECONDS


@pytest.mark.anyio
async def test_content_that_appears_before_the_learned_min_ready_is_awaited():
    silent_polls = polls_at(2.0)
    late_content = [stable_probe()] * silent_polls + [stable_probe(growth=1)]
    tab = ScriptedTab(late_content)
    hints = learned(min_ready=LEARNED_MIN_READY_SECONDS)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS, hints))

    await waiter.wait()

    assert tab.calls > silent_polls
    assert elapsed(tab) == LEARNED_MIN_READY_SECONDS


@pytest.mark.anyio
async def test_learned_min_ready_never_shortens_the_wait_for_a_page_still_growing():
    growing_polls = polls_at(LEARNED_MIN_READY_SECONDS) + 8
    growing = [stable_probe(growth=count) for count in range(growing_polls)]
    tab = ScriptedTab(growing)
    hints = learned(min_ready=1.0)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS, hints))

    stable = await waiter.wait()

    assert stable is True
    assert tab.calls == growing_polls + QUIET_POLLS


@pytest.mark.anyio
async def test_learned_quiet_floor_lengthens_the_quiet_window():
    tab = ScriptedTab([stable_probe(growth=1), stable_probe(growth=2)])
    hints = learned(quiet_floor=LEARNED_QUIET_FLOOR_SECONDS)
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS, hints))

    await waiter.wait()

    assert waiter.quiet_seconds == LEARNED_QUIET_FLOOR_SECONDS
    assert tab.calls == 2 + polls_at(LEARNED_QUIET_FLOOR_SECONDS)


@pytest.mark.anyio
async def test_learned_quiet_floor_wins_over_the_wait_for_cap():
    tab = ScriptedTab([stable_probe()])
    hints = learned(quiet_floor=LEARNED_QUIET_FLOOR_SECONDS)
    waiter = waiter_for(tab, WaitTarget("#price", LONG_BUDGET_SECONDS, hints))

    await waiter.wait()

    assert waiter.quiet_seconds == LEARNED_QUIET_FLOOR_SECONDS
    assert elapsed(tab) == LEARNED_QUIET_FLOOR_SECONDS


@pytest.mark.anyio
async def test_wait_for_shortcut_on_a_restless_page_respects_the_learned_min_ready():
    tab = ScriptedTab(restless_probes())
    hints = learned(min_ready=LEARNED_MIN_READY_SECONDS)
    waiter = waiter_for(tab, WaitTarget("#price", LONG_BUDGET_SECONDS, hints))

    stable = await waiter.wait()

    assert stable is False
    assert waiter.end is ReadinessEnd.WAIT_FOR_FOUND
    assert elapsed(tab) == LEARNED_MIN_READY_SECONDS


@pytest.mark.anyio
async def test_learned_min_ready_beyond_the_budget_returns_a_stable_page_at_the_deadline():
    tab = ScriptedTab([stable_probe()])
    hints = learned(min_ready=LONG_BUDGET_SECONDS)
    waiter = waiter_for(tab, WaitTarget(None, SHORT_BUDGET_SECONDS, hints))

    stable = await waiter.wait()

    assert stable is True
    assert waiter.end is ReadinessEnd.LOAD_BUDGET_EXPIRED
    assert elapsed(tab) == SHORT_BUDGET_SECONDS


@pytest.mark.anyio
async def test_content_timing_reports_last_growth_and_largest_gap():
    observations = [stable_probe(growth=0)] * 3 + [stable_probe(growth=1)] * 3
    tab = ScriptedTab([*observations, stable_probe(growth=2)])
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    first, last = ((index + 1) * POLL_STEP_SECONDS for index in (3, 6))
    assert waiter.content_timing() == ContentTiming(
        last_growth_seconds=last, largest_gap_seconds=last - first
    )


# --- loading ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_interactive_document_counts_as_loaded_after_the_load_watchdog():
    tab = ScriptedTab([stable_probe(ready="interactive")])
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.end is ReadinessEnd.SETTLED
    assert elapsed(tab) == POLL_STEP_SECONDS + LOAD_WATCHDOG_SECONDS


@pytest.mark.anyio
async def test_endless_content_requests_are_ignored_after_the_load_budget():
    budget = 5.0
    polling = {
        index: [request_sent(str(index), API_URL), loading_finished(str(index))]
        for index in range(polls_at(budget))
    }
    tab = ScriptedTab([stable_probe()], polling)
    waiter = waiter_for(tab, WaitTarget(None, budget))

    stable = await waiter.wait()

    assert stable is True
    assert waiter.end is ReadinessEnd.LOAD_BUDGET_EXPIRED
    assert elapsed(tab) == budget * LOAD_BUDGET_SHARE


@pytest.mark.anyio
async def test_failed_observation_during_navigation_counts_as_not_ready():
    navigating = ProtocolException({"message": "Execution context was destroyed", "code": -32000})
    tab = ScriptedTab([stable_probe(), navigating, stable_probe()])
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is True
    assert tab.worlds_created == 2


# --- challenges and the isolated world ----------------------------------------------------


@pytest.mark.anyio
async def test_challenge_holds_the_page_back_and_the_reload_reinstalls_the_observer():
    challenge = [stable_probe(challenge=True)] * 3
    tab = ScriptedTab([*challenge, NAVIGATED, stable_probe(growth=0)])
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is True
    assert waiter.end is ReadinessEnd.SETTLED
    assert waiter.challenge_polls == len(challenge)
    assert tab.worlds_created == 2
    assert tab.calls == len(challenge) + 2 + QUIET_POLLS


@pytest.mark.anyio
async def test_unavailable_isolated_world_falls_back_to_the_main_world(caplog):
    tab = ScriptedTab([stable_probe(growth="500:40")])
    tab.world_fails = True
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    with caplog.at_level(logging.WARNING, logger="render.browser"):
        stable = await waiter.wait()

    warnings = [record for record in caplog.records if "Isolated world" in record.getMessage()]
    assert stable is True
    assert tab.main_world_calls == tab.calls
    assert len(warnings) == 1


@pytest.mark.anyio
async def test_failing_script_in_the_isolated_world_switches_to_the_main_world_for_good(caplog):
    tab = ScriptedTab([stable_probe(growth="500:40")])
    tab.script_fails = True
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    with caplog.at_level(logging.WARNING, logger="render.browser"):
        stable = await waiter.wait()

    assert stable is True
    assert tab.worlds_created == 1
    assert tab.main_world_calls == tab.calls
    assert "Isolated world unavailable" in caplog.text


@pytest.mark.anyio
async def test_main_world_fallback_still_sees_changing_content():
    changing = [stable_probe(growth=f"{length}:40") for length in (10, 200, 800)]
    tab = ScriptedTab(changing)
    tab.world_fails = True
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert tab.calls == len(changing) + QUIET_POLLS


@pytest.mark.anyio
async def test_unresolved_challenge_at_deadline_is_blocked_and_counts_polls():
    tab = ScriptedTab([stable_probe(challenge=True)])
    waiter = waiter_for(tab, WaitTarget(None, SHORT_BUDGET_SECONDS))

    with pytest.raises(TargetBlockedError):
        await waiter.wait()

    assert waiter.end is ReadinessEnd.CHALLENGE
    assert waiter.challenge_polls == tab.calls


# --- deadline and wait_for ----------------------------------------------------------------


@pytest.mark.anyio
async def test_content_still_growing_at_deadline_returns_unstable():
    tab = ScriptedTab(restless_probes())
    waiter = waiter_for(tab, WaitTarget(None, SHORT_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is False
    assert waiter.end is ReadinessEnd.DEADLINE
    assert elapsed(tab) == SHORT_BUDGET_SECONDS


@pytest.mark.anyio
async def test_waits_for_required_element():
    tab = ScriptedTab([stable_probe(found=False)] * 6 + [stable_probe()])
    waiter = waiter_for(tab, WaitTarget("#price", LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is True
    assert tab.calls == 7


@pytest.mark.anyio
async def test_missing_element_at_deadline_is_a_timeout():
    tab = ScriptedTab([stable_probe(found=False)])
    waiter = waiter_for(tab, WaitTarget("#price", SHORT_BUDGET_SECONDS))

    with pytest.raises(RenderTimeoutError, match="Timed out after"):
        await waiter.wait()

    assert waiter.end is ReadinessEnd.ELEMENT_MISSING


@pytest.mark.anyio
async def test_found_element_on_restless_page_returns_unstable_after_the_cap():
    tab = ScriptedTab(restless_probes())
    waiter = waiter_for(tab, WaitTarget("#price", LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is False
    assert waiter.end is ReadinessEnd.WAIT_FOR_FOUND
    assert elapsed(tab) == pytest.approx(POLL_STEP_SECONDS + WAIT_FOR_QUIET_CAP_SECONDS)


@pytest.mark.anyio
async def test_cap_counts_only_from_the_first_usable_poll(quick_cap):
    unusable = [stable_probe(growth=1, ready="loading"), stable_probe(growth=2, challenge=True)]
    tab = ScriptedTab([*unusable, *restless_probes()[2:]])
    waiter = waiter_for(tab, WaitTarget("#price", LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is False
    assert waiter.end is ReadinessEnd.WAIT_FOR_FOUND
    assert elapsed(tab) == pytest.approx(
        (len(unusable) + 1) * POLL_STEP_SECONDS + QUICK_CAP_SECONDS
    )


@pytest.mark.anyio
async def test_restless_page_without_wait_for_is_not_cut_short(quick_cap):
    tab = ScriptedTab(restless_probes())
    waiter = waiter_for(tab, WaitTarget(None, SHORT_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is False
    assert elapsed(tab) == SHORT_BUDGET_SECONDS


# --- deadline decided on the last successful observation ----------------------------------

# The poll that lands exactly on the short budget's deadline.
DEADLINE_POLL = polls_at(SHORT_BUDGET_SECONDS) - 1


def document_swap() -> ProtocolException:
    return ProtocolException({"message": "Execution context was destroyed", "code": -32000})


@pytest.mark.anyio
async def test_document_swap_at_the_deadline_without_wait_for_returns_the_page_unstable():
    restless = [stable_probe(growth=count) for count in range(DEADLINE_POLL)]
    tab = ScriptedTab([*restless, document_swap()])
    waiter = waiter_for(tab, WaitTarget(None, SHORT_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is False
    assert waiter.end is ReadinessEnd.DEADLINE


@pytest.mark.anyio
async def test_document_swap_at_the_deadline_still_reports_the_challenge():
    challenge = [stable_probe(challenge=True)] * DEADLINE_POLL
    tab = ScriptedTab([*challenge, document_swap()])
    waiter = waiter_for(tab, WaitTarget(None, SHORT_BUDGET_SECONDS))

    with pytest.raises(TargetBlockedError):
        await waiter.wait()

    assert waiter.end is ReadinessEnd.CHALLENGE


@pytest.mark.anyio
async def test_page_that_could_never_be_read_is_a_navigation_error():
    tab = ScriptedTab([document_swap()])
    waiter = waiter_for(tab, WaitTarget(None, SHORT_BUDGET_SECONDS))

    with pytest.raises(NavigationError, match="could not be read"):
        await waiter.wait()


@pytest.mark.anyio
async def test_crashed_tab_fails_early_instead_of_waiting_for_the_deadline():
    crashed = ProtocolException({"message": "Target closed", "code": -32000})
    tab = ScriptedTab([crashed])
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    with pytest.raises(NavigationError, match="crashed or was closed"):
        await waiter.wait()

    assert tab.calls == readiness.MAX_TARGET_GONE_FAILURES


@pytest.mark.anyio
async def test_document_swaps_alone_never_fail_early():
    tab = ScriptedTab([document_swap()] * 20 + [stable_probe()])
    waiter = waiter_for(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    stable = await waiter.wait()

    assert stable is True


# --- missing element -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_missing_element_is_awaited_while_a_challenge_is_shown():
    tab = ScriptedTab([stable_probe(found=False, challenge=True)])
    waiter = waiter_for(tab, WaitTarget("#price", SHORT_BUDGET_SECONDS))

    with pytest.raises(TargetBlockedError):
        await waiter.wait()


@pytest.mark.anyio
async def test_idle_page_without_element_is_awaited_until_the_deadline():
    # A quiet page does not prove nothing is coming: content scheduled by a timer or
    # pushed over a websocket arrives without prior activity, so there is no early give-up.
    tab = ScriptedTab([stable_probe(found=False)])
    waiter = waiter_for(tab, WaitTarget("#price", SHORT_BUDGET_SECONDS))

    with pytest.raises(RenderTimeoutError, match="Timed out after"):
        await waiter.wait()

    assert waiter.end is ReadinessEnd.ELEMENT_MISSING
    assert elapsed(tab) == pytest.approx(SHORT_BUDGET_SECONDS)


# --- observer script and placeholder definitions ------------------------------------------


def test_observer_script_encodes_selector_as_json_string():
    script = build_observer_script('a[title="x"]"); alert(1); ("', observe=True)

    assert '"a[title=\\"x\\"]\\"); alert(1); (\\""' in script


def test_observer_script_uses_the_challenge_definitions_of_completeness():
    script = build_observer_script(None, observe=True)

    assert json.dumps(HUMAN_VERIFICATION_PATTERN.pattern) in script
    assert json.dumps(CHALLENGE_TITLE_PATTERN.pattern) in script
    assert json.dumps([list(group) for group in CHALLENGE_SDK_MARKERS]) in script
    assert json.dumps(list(CHALLENGE_BODY_MARKERS)) in script
    assert f'"interstitialMaxText": {INTERSTITIAL_MAX_TEXT_CHARS}' in script


def test_observer_script_uses_the_placeholder_definitions():
    script = build_observer_script(None, observe=False)

    assert json.dumps(LOADING_PLACEHOLDER_NAME_PATTERN.pattern) in script
    assert json.dumps(LOADING_TEXT_PATTERN.pattern) in script
    assert json.dumps(placeholder_candidate_selector()) in script
    assert '"observe": false' in script


@pytest.mark.parametrize(
    "name",
    ["page-loader", "is-loading", "Spinner_root__x1", "skeleton", "card placeholder-shimmer"],
)
def test_placeholder_names_match_as_their_own_token(name):
    assert LOADING_PLACEHOLDER_NAME_PATTERN.search(name)


@pytest.mark.parametrize("name", ["file-uploader", "lazyloading", "downloader", "loaded"])
def test_words_merely_containing_a_placeholder_name_do_not_match(name):
    assert LOADING_PLACEHOLDER_NAME_PATTERN.search(name) is None


@pytest.mark.parametrize(
    "text", ["Loading", "Loading...", "Loading…", "loading …", "Wird geladen", "Lädt…", "Laedt..."]
)
def test_loading_texts_match(text):
    assert LOADING_TEXT_PATTERN.match(text)


@pytest.mark.parametrize("text", ["Loading times explained", "Download", "Upload complete"])
def test_ordinary_texts_are_not_loading_texts(text):
    assert LOADING_TEXT_PATTERN.match(text) is None


def test_placeholder_candidates_include_busy_regions():
    assert placeholder_candidate_selector().startswith('[aria-busy="true"],')
