import pytest

from app.timing import Note, Phase, PhaseTimer


class ManualClock:
    """A monotonic clock that only moves when the test advances it."""

    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


def test_phase_duration_is_recorded_in_milliseconds(clock):
    timer = PhaseTimer(clock)

    with timer.phase(Phase.QUEUE):
        clock.advance(0.25)

    assert timer.durations_ms() == {"queue": 250, "total": 250}


def test_total_includes_time_outside_any_phase(clock):
    timer = PhaseTimer(clock)
    clock.advance(1.0)

    with timer.phase(Phase.NAVIGATE):
        clock.advance(0.5)
    clock.advance(0.1)

    assert timer.durations_ms()["total"] == 1600


def test_repeated_phase_accumulates(clock):
    timer = PhaseTimer(clock)

    with timer.phase(Phase.CONTEXT):
        clock.advance(0.01)
    with timer.phase(Phase.CONTEXT):
        clock.advance(0.02)

    assert timer.durations_ms()["context"] == 30


def test_phase_is_recorded_when_the_block_raises(clock):
    timer = PhaseTimer(clock)

    with pytest.raises(TimeoutError), timer.phase(Phase.READINESS):
        clock.advance(2.0)
        raise TimeoutError

    assert timer.durations_ms()["readiness"] == 2000


def test_summary_lists_phases_in_order_then_total_then_notes(clock):
    timer = PhaseTimer(clock)
    with timer.phase(Phase.HOST_WAIT):
        clock.advance(0.001)
    with timer.phase(Phase.READINESS):
        clock.advance(3.0)
    timer.note(Note.READINESS_END, "settled")
    timer.note(Note.CHALLENGE_POLLS, 0)

    summary = timer.summary()

    assert summary == (
        "host_wait_ms=1 readiness_ms=3000 total_ms=3001 readiness_end=settled challenge_polls=0"
    )


def test_phases_that_did_not_run_are_absent(clock):
    timer = PhaseTimer(clock)

    assert timer.durations_ms() == {"total": 0}
    assert timer.notes() == {}
