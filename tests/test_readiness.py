import asyncio
import json

import pytest
from zendriver.core.connection import ProtocolException

from app.browser.readiness import (
    STABLE_POLLS,
    ReadinessEnd,
    ReadinessTimings,
    ReadinessWaiter,
    WaitTarget,
    build_probe_script,
)
from app.content.completeness import (
    CHALLENGE_BODY_MARKERS,
    CHALLENGE_SDK_MARKERS,
    CHALLENGE_TITLE_PATTERN,
    HUMAN_VERIFICATION_PATTERN,
    INTERSTITIAL_MAX_TEXT_CHARS,
)
from app.errors import RenderTimeoutError, TargetBlockedError
from tests.fakes import stable_probe

SHORT_BUDGET_SECONDS = 0.05
LONG_BUDGET_SECONDS = 30
QUICK_TIMINGS = ReadinessTimings(found_settle_seconds=0.02, idle_give_up_seconds=0.02)


class ScriptedTab:
    """Returns the given probe results in order, repeating the last one."""

    def __init__(self, probes: list[dict]):
        self._probes = probes
        self.calls = 0

    async def evaluate(self, expression: str):
        probe = self._probes[min(self.calls, len(self._probes) - 1)]
        self.calls += 1
        if isinstance(probe, Exception):
            raise probe
        return probe


@pytest.mark.anyio
async def test_page_is_ready_once_content_stops_changing():
    growing = [stable_probe(text_length=length) for length in (10, 200, 800)]
    tab = ScriptedTab(growing + [stable_probe(text_length=800)])

    stable = await ReadinessWaiter(tab, WaitTarget(None, 5)).wait()

    assert stable is True
    assert tab.calls == len(growing) + STABLE_POLLS - 1


@pytest.mark.anyio
async def test_waits_for_required_element():
    tab = ScriptedTab([stable_probe(found=False)] * 3 + [stable_probe(found=True)])

    stable = await ReadinessWaiter(tab, WaitTarget("#price", 5)).wait()

    assert stable is True
    assert tab.calls >= 4


@pytest.mark.anyio
async def test_missing_element_at_deadline_is_a_timeout():
    tab = ScriptedTab([stable_probe(found=False)])

    with pytest.raises(RenderTimeoutError, match="#price"):
        await ReadinessWaiter(tab, WaitTarget("#price", SHORT_BUDGET_SECONDS)).wait()


@pytest.mark.anyio
async def test_unresolved_challenge_at_deadline_is_blocked():
    tab = ScriptedTab([stable_probe(challenge=True)])

    with pytest.raises(TargetBlockedError):
        await ReadinessWaiter(tab, WaitTarget(None, SHORT_BUDGET_SECONDS)).wait()


@pytest.mark.anyio
async def test_challenge_that_resolves_leads_to_ready_page():
    tab = ScriptedTab([stable_probe(challenge=True)] * 3 + [stable_probe(text_length=900)])

    stable = await ReadinessWaiter(tab, WaitTarget(None, 5)).wait()

    assert stable is True


@pytest.mark.anyio
async def test_failed_evaluation_during_navigation_counts_as_not_ready():
    navigating = ProtocolException({"message": "Execution context was destroyed", "code": -32000})
    tab = ScriptedTab([stable_probe(), navigating, stable_probe()])

    stable = await ReadinessWaiter(tab, WaitTarget(None, 5)).wait()

    assert stable is True
    assert tab.calls == 2 + STABLE_POLLS


@pytest.mark.anyio
async def test_new_network_responses_keep_the_page_unstable():
    loading = [stable_probe(requestCount=count) for count in (1, 2, 3)]
    tab = ScriptedTab(loading + [stable_probe(requestCount=3)])

    stable = await ReadinessWaiter(tab, WaitTarget(None, 5)).wait()

    assert stable is True
    assert tab.calls == len(loading) + STABLE_POLLS - 1


@pytest.mark.anyio
async def test_endless_polling_does_not_block_readiness_after_load_budget():
    polling = [stable_probe(requestCount=count) for count in range(1, 100_000)]
    tab = ScriptedTab(polling)

    stable = await ReadinessWaiter(tab, WaitTarget(None, SHORT_BUDGET_SECONDS)).wait()

    assert stable is True


@pytest.mark.anyio
async def test_content_still_changing_at_deadline_returns_unstable():
    endless = [stable_probe(text_length=length) for length in range(1, 100_000)]
    tab = ScriptedTab(endless)

    stable = await ReadinessWaiter(tab, WaitTarget(None, SHORT_BUDGET_SECONDS)).wait()

    assert stable is False


def test_probe_script_encodes_selector_as_json_string():
    script = build_probe_script('a[title="x"]"); alert(1); ("')

    assert '"a[title=\\"x\\"]\\"); alert(1); (\\""' in script


@pytest.mark.anyio
async def test_missing_element_on_idle_loaded_page_fails_before_the_deadline():
    tab = ScriptedTab([stable_probe(found=False)])
    waiter = ReadinessWaiter(tab, WaitTarget("#price", LONG_BUDGET_SECONDS), QUICK_TIMINGS)

    with pytest.raises(RenderTimeoutError, match="stayed idle"):
        await asyncio.wait_for(waiter.wait(), timeout=5)


@pytest.mark.anyio
async def test_missing_element_is_awaited_while_page_is_still_active():
    busy = [stable_probe(found=False, requestCount=count) for count in range(1, 100_000)]
    tab = ScriptedTab(busy)
    waiter = ReadinessWaiter(tab, WaitTarget("#price", SHORT_BUDGET_SECONDS), QUICK_TIMINGS)

    with pytest.raises(RenderTimeoutError, match="Timed out after"):
        await waiter.wait()


@pytest.mark.anyio
async def test_missing_element_is_awaited_while_a_challenge_is_shown():
    tab = ScriptedTab([stable_probe(found=False, challenge=True)])
    waiter = ReadinessWaiter(tab, WaitTarget("#price", SHORT_BUDGET_SECONDS), QUICK_TIMINGS)

    with pytest.raises(TargetBlockedError):
        await waiter.wait()


@pytest.mark.anyio
async def test_found_element_on_restless_page_returns_before_the_deadline():
    restless = [stable_probe(text_length=length) for length in range(1, 100_000)]
    tab = ScriptedTab(restless)
    waiter = ReadinessWaiter(tab, WaitTarget("#price", LONG_BUDGET_SECONDS), QUICK_TIMINGS)

    stable = await asyncio.wait_for(waiter.wait(), timeout=5)

    assert stable is False


@pytest.mark.anyio
async def test_restless_page_without_wait_for_is_not_cut_short():
    restless = [stable_probe(text_length=length) for length in range(1, 100_000)]
    tab = ScriptedTab(restless)
    waiter = ReadinessWaiter(tab, WaitTarget(None, 0.2), QUICK_TIMINGS)
    loop = asyncio.get_running_loop()
    started = loop.time()

    stable = await waiter.wait()

    assert stable is False
    assert loop.time() - started >= 0.2


@pytest.mark.anyio
async def test_idle_page_without_element_is_awaited_until_deadline_by_default():
    tab = ScriptedTab([stable_probe(found=False)])
    waiter = ReadinessWaiter(tab, WaitTarget("#price", SHORT_BUDGET_SECONDS))

    with pytest.raises(RenderTimeoutError, match="Timed out after"):
        await waiter.wait()


@pytest.mark.anyio
async def test_page_settled_within_load_budget_ends_as_settled():
    tab = ScriptedTab([stable_probe()])
    waiter = ReadinessWaiter(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.end is ReadinessEnd.SETTLED
    assert waiter.challenge_polls == 0


@pytest.mark.anyio
async def test_page_settled_only_after_load_budget_ends_as_load_budget_expired():
    polling = [stable_probe(requestCount=count) for count in range(1, 100_000)]
    waiter = ReadinessWaiter(ScriptedTab(polling), WaitTarget(None, SHORT_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.end is ReadinessEnd.LOAD_BUDGET_EXPIRED


@pytest.mark.anyio
async def test_found_element_on_restless_page_ends_as_wait_for_found():
    restless = [stable_probe(text_length=length) for length in range(1, 100_000)]
    waiter = ReadinessWaiter(
        ScriptedTab(restless), WaitTarget("#price", LONG_BUDGET_SECONDS), QUICK_TIMINGS
    )

    await asyncio.wait_for(waiter.wait(), timeout=5)

    assert waiter.end is ReadinessEnd.WAIT_FOR_FOUND


@pytest.mark.anyio
async def test_changing_content_at_deadline_ends_as_deadline():
    endless = [stable_probe(text_length=length) for length in range(1, 100_000)]
    waiter = ReadinessWaiter(ScriptedTab(endless), WaitTarget(None, SHORT_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.end is ReadinessEnd.DEADLINE


@pytest.mark.anyio
async def test_unresolved_challenge_ends_as_challenge_and_counts_polls():
    tab = ScriptedTab([stable_probe(challenge=True)])
    waiter = ReadinessWaiter(tab, WaitTarget(None, SHORT_BUDGET_SECONDS))

    with pytest.raises(TargetBlockedError):
        await waiter.wait()

    assert waiter.end is ReadinessEnd.CHALLENGE
    assert waiter.challenge_polls == tab.calls


@pytest.mark.anyio
async def test_resolved_challenge_is_still_counted():
    tab = ScriptedTab([stable_probe(challenge=True)] * 3 + [stable_probe(text_length=900)])
    waiter = ReadinessWaiter(tab, WaitTarget(None, LONG_BUDGET_SECONDS))

    await waiter.wait()

    assert waiter.end is ReadinessEnd.SETTLED
    assert waiter.challenge_polls == 3


@pytest.mark.anyio
async def test_missing_element_at_deadline_ends_as_element_missing():
    waiter = ReadinessWaiter(
        ScriptedTab([stable_probe(found=False)]), WaitTarget("#price", SHORT_BUDGET_SECONDS)
    )

    with pytest.raises(RenderTimeoutError):
        await waiter.wait()

    assert waiter.end is ReadinessEnd.ELEMENT_MISSING


@pytest.mark.anyio
async def test_idle_page_without_element_ends_as_idle_give_up():
    waiter = ReadinessWaiter(
        ScriptedTab([stable_probe(found=False)]),
        WaitTarget("#price", LONG_BUDGET_SECONDS),
        QUICK_TIMINGS,
    )

    with pytest.raises(RenderTimeoutError):
        await asyncio.wait_for(waiter.wait(), timeout=5)

    assert waiter.end is ReadinessEnd.IDLE_GIVE_UP


def test_probe_script_uses_the_challenge_definitions_of_completeness():
    script = build_probe_script(None)

    assert json.dumps(HUMAN_VERIFICATION_PATTERN.pattern) in script
    assert json.dumps(CHALLENGE_TITLE_PATTERN.pattern) in script
    assert json.dumps([list(group) for group in CHALLENGE_SDK_MARKERS]) in script
    assert json.dumps(list(CHALLENGE_BODY_MARKERS)) in script
    assert f'"interstitialMaxText": {INTERSTITIAL_MAX_TEXT_CHARS}' in script


@pytest.mark.anyio
async def test_zero_settle_window_returns_on_first_poll_with_element_on_loaded_unchallenged_page():
    unusable = [
        stable_probe(text_length=1, ready="loading"),
        stable_probe(text_length=2, challenge=True),
    ]
    tab = ScriptedTab(unusable + [stable_probe(text_length=length) for length in range(3, 100)])
    timings = ReadinessTimings(found_settle_seconds=0)

    stable = await ReadinessWaiter(tab, WaitTarget("#price", LONG_BUDGET_SECONDS), timings).wait()

    assert stable is False
    assert tab.calls == len(unusable) + 1
