import asyncio

import pytest
from zendriver.core.connection import ProtocolException

import app.browser.readiness as readiness
from app.browser.activity import ActivityLog
from app.browser.late_content import WatchInconclusiveError
from app.browser.readiness import (
    BASE_QUIET_SECONDS,
    ReadinessEnd,
    ReadinessOptions,
    ReadinessWaiter,
    WaitTarget,
)
from tests.fakes import NAVIGATED, POLL_STEP_SECONDS, ScriptedTab, stable_probe

BUDGET_SECONDS = 30.0
OBSERVE_SECONDS = 10.0
# The timer of quotes.toscrape.com/js-delayed/ fires after about 10 s of silence.
SILENT_POLLS = round(9.5 / POLL_STEP_SECONDS)


@pytest.fixture(autouse=True)
def production_quiet_window(monkeypatch):
    monkeypatch.setattr(readiness, "BASE_QUIET_SECONDS", BASE_QUIET_SECONDS)


def silent_timer_page() -> list[dict]:
    return [stable_probe(growth=0)] * SILENT_POLLS + [stable_probe(growth=1)]


async def finished_waiter(tab: ScriptedTab) -> ReadinessWaiter:
    options = ReadinessOptions(ActivityLog(tab.clock))
    waiter = ReadinessWaiter(tab, WaitTarget(None, BUDGET_SECONDS), options)
    await waiter.wait()
    return waiter


@pytest.mark.anyio
async def test_silent_timer_page_returns_early_and_the_watch_sees_its_late_content():
    tab = ScriptedTab(silent_timer_page())
    started = tab.clock.now
    waiter = await finished_waiter(tab)
    returned_after = tab.clock.now - started

    late = await waiter.late_watch().watch(OBSERVE_SECONDS)

    assert waiter.end is ReadinessEnd.SETTLED
    assert returned_after == BASE_QUIET_SECONDS
    assert late == (SILENT_POLLS + 1) * POLL_STEP_SECONDS


@pytest.mark.anyio
async def test_watch_of_a_finished_page_reports_no_late_content():
    tab = ScriptedTab([stable_probe()])
    waiter = await finished_waiter(tab)

    late = await waiter.late_watch().watch(OBSERVE_SECONDS)

    assert late is None


@pytest.mark.anyio
async def test_watch_stops_after_the_observation_time():
    tab = ScriptedTab([stable_probe()])
    waiter = await finished_waiter(tab)
    started = tab.clock.now

    await waiter.late_watch().watch(OBSERVE_SECONDS)

    assert tab.clock.now - started == OBSERVE_SECONDS


@pytest.mark.anyio
async def test_replaced_document_during_the_watch_counts_as_late_content():
    tab = ScriptedTab([stable_probe()] * 8 + [NAVIGATED, stable_probe()])
    waiter = await finished_waiter(tab)

    late = await waiter.late_watch().watch(OBSERVE_SECONDS)

    assert late is not None
    assert tab.worlds_created == 2


@pytest.mark.anyio
async def test_browser_failure_during_the_watch_propagates():
    broken = ConnectionError("websocket closed")
    tab = ScriptedTab([stable_probe()] * 8 + [broken])
    waiter = await finished_waiter(tab)

    with pytest.raises(ConnectionError, match="websocket closed"):
        await waiter.late_watch().watch(OBSERVE_SECONDS)


@pytest.mark.anyio
async def test_stopped_watch_ends_without_observing():
    tab = ScriptedTab(silent_timer_page())
    waiter = await finished_waiter(tab)
    observed_before = tab.calls
    stop = asyncio.Event()
    stop.set()

    late = await waiter.late_watch().watch(OBSERVE_SECONDS, stop)

    assert late is None
    assert tab.calls == observed_before


def test_late_watch_before_wait_is_a_programming_error():
    waiter = ReadinessWaiter(ScriptedTab([stable_probe()]), WaitTarget(None, BUDGET_SECONDS))

    with pytest.raises(RuntimeError, match="finished wait"):
        waiter.late_watch()


@pytest.mark.anyio
async def test_watch_that_never_reads_the_page_is_inconclusive_not_clean():
    swap = ProtocolException({"message": "Execution context was destroyed", "code": -32000})
    tab = ScriptedTab([stable_probe(), stable_probe(), swap])
    waiter = await finished_waiter(tab)

    with pytest.raises(WatchInconclusiveError):
        await waiter.late_watch().watch(OBSERVE_SECONDS)


@pytest.mark.anyio
async def test_no_watch_after_a_wait_in_the_main_world_fallback():
    # Its growth token is text length and node count: a rotating carousel would count
    # as late content, so the fallback does not watch at all.
    tab = ScriptedTab([stable_probe(growth="500:40")])
    tab.world_fails = True
    waiter = await finished_waiter(tab)

    assert waiter.late_watch() is None
