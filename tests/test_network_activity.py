import pytest
from zendriver import cdp

from app.browser.activity import ActivityLog
from app.browser.network_activity import LONG_POLL_SECONDS, InflightTracker
from tests.fakes import (
    CdpEvents,
    ManualClock,
    loading_failed,
    loading_finished,
    request_sent,
    response_received,
)

API_URL = "https://shop.example/api/products"
TRACKERS = frozenset({"google-analytics.com", "doubleclick.net"})
STEP_SECONDS = 0.2


def attached_tracker() -> tuple[InflightTracker, CdpEvents, ManualClock, ActivityLog]:
    clock = ManualClock()
    activity = ActivityLog(clock)
    tab = CdpEvents()
    tracker = InflightTracker(TRACKERS, activity)
    tracker.attach(tab)
    return tracker, tab, clock, activity


@pytest.mark.anyio
async def test_content_request_is_in_flight_until_it_finishes():
    tracker, tab, _, _ = attached_tracker()

    await tab.emit(request_sent("1", API_URL, "XHR"))
    during = tracker.snapshot().inflight
    await tab.emit(loading_finished("1"))

    assert during == 1
    assert tracker.snapshot().inflight == 0


@pytest.mark.anyio
@pytest.mark.parametrize("resource_type", ["Document", "XHR", "Fetch", "Script"])
async def test_content_resource_types_are_counted(resource_type):
    tracker, tab, _, _ = attached_tracker()

    await tab.emit(request_sent("1", API_URL, resource_type))

    assert tracker.snapshot().inflight == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "resource_type",
    [
        "Ping",
        "EventSource",
        "WebSocket",
        "Image",
        "Font",
        "Media",
        "Stylesheet",
        "Prefetch",
        "Other",
    ],
)
async def test_non_content_requests_are_ignored(resource_type):
    tracker, tab, _, activity = attached_tracker()

    await tab.emit(request_sent("1", API_URL, resource_type))

    assert tracker.snapshot().inflight == 0
    assert tracker.snapshot().ignored == 1
    assert activity.last is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "https://www.google-analytics.com/g/collect",
        "https://google-analytics.com/collect",
        "https://stats.g.doubleclick.net/j/collect",
    ],
)
async def test_tracker_hosts_and_their_subdomains_are_ignored(url):
    tracker, tab, _, _ = attached_tracker()

    await tab.emit(request_sent("1", url, "Script"))

    assert tracker.snapshot().inflight == 0
    assert tracker.snapshot().ignored == 1


@pytest.mark.anyio
async def test_host_that_only_ends_like_a_tracker_is_counted():
    tracker, tab, _, _ = attached_tracker()

    await tab.emit(request_sent("1", "https://notdoubleclick.net/api", "XHR"))

    assert tracker.snapshot().inflight == 1


@pytest.mark.anyio
async def test_failed_request_is_no_longer_in_flight():
    tracker, tab, _, _ = attached_tracker()
    await tab.emit(request_sent("1", API_URL))

    await tab.emit(loading_failed("1"))

    assert tracker.snapshot().inflight == 0


@pytest.mark.anyio
async def test_request_open_longer_than_long_poll_limit_stops_counting():
    tracker, tab, clock, _ = attached_tracker()
    await tab.emit(request_sent("1", API_URL))

    clock.advance(LONG_POLL_SECONDS)

    assert tracker.snapshot().inflight == 0
    assert tracker.snapshot().ignored == 1


@pytest.mark.anyio
async def test_finish_of_a_long_poll_is_not_content_activity():
    tracker, tab, clock, activity = attached_tracker()
    await tab.emit(request_sent("1", API_URL))
    started = activity.last
    clock.advance(LONG_POLL_SECONDS + STEP_SECONDS)

    await tab.emit(loading_finished("1"))

    assert activity.last == started
    assert tracker.snapshot().ignored == 1


@pytest.mark.anyio
async def test_event_stream_stops_counting_once_its_response_arrives():
    tracker, tab, _, _ = attached_tracker()
    await tab.emit(request_sent("1", API_URL, "Fetch"))

    await tab.emit(response_received("1", "EventSource"))

    assert tracker.snapshot().inflight == 0
    assert tracker.snapshot().ignored == 1


@pytest.mark.anyio
async def test_idle_gap_before_a_new_request_counts_towards_the_rhythm():
    tracker, tab, clock, activity = attached_tracker()
    await tab.emit(request_sent("1", API_URL))
    await tab.emit(loading_finished("1"))
    clock.advance(STEP_SECONDS)

    await tab.emit(request_sent("2", API_URL))

    assert activity.last == clock.now
    assert activity.max_gap == pytest.approx(STEP_SECONDS)


@pytest.mark.anyio
async def test_time_spent_waiting_for_a_slow_response_is_not_an_idle_gap():
    tracker, tab, clock, activity = attached_tracker()
    await tab.emit(request_sent("1", API_URL))
    clock.advance(STEP_SECONDS)

    await tab.emit(loading_finished("1"))

    assert activity.last == clock.now
    assert activity.max_gap == 0.0


@pytest.mark.anyio
async def test_gap_before_a_request_that_starts_while_another_is_open_is_not_idle():
    tracker, tab, clock, activity = attached_tracker()
    await tab.emit(request_sent("1", API_URL))
    clock.advance(STEP_SECONDS)

    await tab.emit(request_sent("2", API_URL))

    assert activity.max_gap == 0.0


@pytest.mark.anyio
async def test_redirect_hop_keeps_one_request_in_flight():
    tracker, tab, _, _ = attached_tracker()

    await tab.emit(request_sent("1", API_URL, "Document"))
    await tab.emit(request_sent("1", "https://shop.example/home", "Document"))

    assert tracker.snapshot().inflight == 1


@pytest.mark.anyio
async def test_finish_of_an_unknown_request_changes_nothing():
    tracker, tab, _, activity = attached_tracker()

    await tab.emit(loading_finished("never-started"))

    assert tracker.snapshot().inflight == 0
    assert activity.last is None


@pytest.mark.anyio
async def test_detached_tracker_no_longer_sees_events():
    tracker, tab, _, _ = attached_tracker()

    tracker.detach(tab)
    await tab.emit(request_sent("1", API_URL))

    assert tracker.snapshot().inflight == 0
    assert all(not handlers for handlers in tab.handlers.values())


def test_tracker_listens_to_the_four_network_events():
    tracker, tab, _, _ = attached_tracker()

    assert set(tab.handlers) == {
        cdp.network.RequestWillBeSent,
        cdp.network.ResponseReceived,
        cdp.network.LoadingFinished,
        cdp.network.LoadingFailed,
    }
