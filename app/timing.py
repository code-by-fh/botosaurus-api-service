"""Per-request phase durations, logged as one line to show where render time goes.

A ``PhaseTimer`` travels with the request objects (``ScrapeRequest`` and
``BrowserJob``) so each component times its own phase without knowing about
the log line. One timer belongs to one request and is only touched from the
event loop, so it needs no locking.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum

Clock = Callable[[], float]

MILLISECONDS_PER_SECOND = 1000
TOTAL_KEY = "total"


class Phase(Enum):
    """Timed sections of a request, in the order they usually run."""

    HOST_WAIT = "host_wait"
    HTTP = "http"
    QUEUE = "queue"
    RESTART = "restart"
    CONTEXT = "context"
    NAVIGATE = "navigate"
    READINESS = "readiness"
    READ = "read"
    OUTPUT = "output"


class Note(Enum):
    """Non-duration facts about a request that explain its timing."""

    READINESS_END = "readiness_end"
    CHALLENGE_POLLS = "challenge_polls"
    QUIET_MS = "quiet_ms"
    INFLIGHT_IGNORED = "inflight_ignored"
    CLEARANCE = "clearance"
    BLOCKED = "blocked"
    PROFILE = "profile"
    MIN_READY_MS = "min_ready_ms"


class PhaseTimer:
    """Accumulates the duration of each phase and a few notes for one request."""

    def __init__(self, clock: Clock = time.monotonic):
        self._clock = clock
        self._started = clock()
        self._seconds: dict[Phase, float] = {}
        self._notes: dict[Note, str] = {}

    @contextmanager
    def phase(self, phase: Phase) -> Iterator[None]:
        """Time the ``with`` block as ``phase``; also when the block raises.

        A phase entered more than once accumulates its durations.
        """
        started = self._clock()
        try:
            yield
        finally:
            elapsed = self._clock() - started
            self._seconds[phase] = self._seconds.get(phase, 0.0) + elapsed

    def note(self, note: Note, value: object) -> None:
        """Record ``value`` for ``note``, replacing an earlier value."""
        self._notes[note] = str(value)

    def durations_ms(self) -> dict[str, int]:
        """Milliseconds per phase that ran, in the order first seen, plus the total so far."""
        durations = {phase.value: _milliseconds(spent) for phase, spent in self._seconds.items()}
        durations[TOTAL_KEY] = _milliseconds(self._clock() - self._started)
        return durations

    def notes(self) -> dict[str, str]:
        """The recorded notes, keyed by name."""
        return {note.value: value for note, value in self._notes.items()}

    def summary(self) -> str:
        """``key=value`` pairs: ``<phase>_ms`` for each phase that ran, ``total_ms``, notes."""
        pairs = [f"{name}_ms={value}" for name, value in self.durations_ms().items()]
        pairs += [f"{name}={value}" for name, value in self.notes().items()]
        return " ".join(pairs)


def _milliseconds(seconds: float) -> int:
    return round(seconds * MILLISECONDS_PER_SECOND)
