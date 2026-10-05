"""Learned readiness timing per site section, so slow sites get enough time without knobs.

Some pages insert their content from a timer after a period of complete silence
that is longer than any quiet window (``quotes.toscrape.com/js-delayed/`` is
silent for about 10 s). A single render cannot tell such a page from a finished
one. This store remembers, per section (``verdicts.section_key``), when content
grew in recent renders, including growth seen *after* a render was returned
(``browser.late_content``), and derives two floors for the next render:

- ``min_ready_seconds``: the 90th percentile of the last growth times plus
  ``MIN_READY_MARGIN_SECONDS``; the page is not declared ready before it;
- ``quiet_floor_seconds``: ``QUIET_GAP_FACTOR`` times the 90th percentile of the
  largest idle gaps, capped at ``MAX_QUIET_SECONDS``.

Learned data may only lengthen waits: the floors are lower bounds, and only
observations of the current render may end a wait. The percentile ignores the
slowest tenth of the samples, so a single outlier does not slow every render of
a section down. Profiles live in memory, per process, and expire
``ttl_seconds`` after their first render so changed sites are re-learned.
"""

import itertools
import logging
import math
import random
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.browser.readiness import (
    MAX_QUIET_SECONDS,
    QUIET_GAP_FACTOR,
    ContentTiming,
    ReadinessHints,
)
from app.timing import Clock

log = logging.getLogger("render.profiles")

MAX_PROFILED_SECTIONS = 10_000
PROFILE_WINDOW_SAMPLES = 20
READY_PERCENTILE = 0.9
# Covers the poll interval and small jitter of the site's timer between renders.
MIN_READY_MARGIN_SECONDS = 0.5
# Every section starts with this many observed renders; afterwards only a sample is observed.
PROFILE_OBSERVE_FIRST_RENDERS = 2

ChanceSource = Callable[[], float]


@dataclass(frozen=True)
class ProfilePolicy:
    """How long profiles live and how often renders are observed after their return."""

    ttl_seconds: float
    observe_seconds: float
    sample_rate: float


@dataclass(frozen=True)
class SampleRef:
    """Identifies one recorded render, so its late observation can amend it."""

    key: str
    sequence: int


class _Profile:
    """Recent samples of one section; only touched from the event loop."""

    def __init__(self, expires_at: float):
        self.expires_at = expires_at
        self.samples: deque[tuple[int, ContentTiming]] = deque(maxlen=PROFILE_WINDOW_SAMPLES)
        self.observations = 0

    def amend(self, sequence: int, late_growth_seconds: float) -> None:
        """Raise the last growth of sample ``sequence``; re-add it if it was rotated out."""
        for index, (number, sample) in enumerate(self.samples):
            if number == sequence:
                raised = max(sample.last_growth_seconds, late_growth_seconds)
                self.samples[index] = (number, replace(sample, last_growth_seconds=raised))
                return
        # Late content is the evidence this store exists for; losing it would let the
        # next render return early again.
        self.samples.append((sequence, ContentTiming(late_growth_seconds, 0.0)))

    def hints(self) -> ReadinessHints:
        timings = [sample for _, sample in self.samples]
        growth = _percentile([sample.last_growth_seconds for sample in timings])
        gap = _percentile([sample.largest_gap_seconds for sample in timings])
        return ReadinessHints(
            min_ready_seconds=growth + MIN_READY_MARGIN_SECONDS,
            quiet_floor_seconds=min(MAX_QUIET_SECONDS, QUIET_GAP_FACTOR * gap),
        )


def _percentile(values: list[float]) -> float:
    # Nearest rank: with fewer than ten samples this is the maximum.
    ordered = sorted(values)
    rank = max(1, math.ceil(READY_PERCENTILE * len(ordered)))
    return ordered[rank - 1]


class SectionProfileStore:
    """Bounded, expiring in-memory store of per-section readiness timing."""

    def __init__(
        self,
        policy: ProfilePolicy,
        clock: Clock = time.monotonic,
        chance: ChanceSource = random.random,
    ):
        """:param chance: uniform random numbers in [0, 1) that decide sampled observations."""
        self._policy = policy
        self._clock = clock
        self._chance = chance
        self._sequence = itertools.count()
        self._profiles: OrderedDict[str, _Profile] = OrderedDict()

    def hints(self, key: str) -> ReadinessHints | None:
        """The learned floors for ``key``, or ``None`` if the section is unknown (cold)."""
        profile = self._live(key)
        return profile.hints() if profile and profile.samples else None

    def observe_seconds_for(self, key: str, unverified: bool = False) -> float:
        """How long the next render of ``key`` should be observed after its return (0: not).

        :param unverified: the section still waits for an HTTP verdict. Only a cleanly
            observed render may serve as verification reference, so such a section is
            observed on every render, not only on sampled ones.
        """
        if self._policy.observe_seconds <= 0:
            return 0.0
        profile = self._live(key)
        observed = profile.observations if profile else 0
        if unverified or observed < PROFILE_OBSERVE_FIRST_RENDERS or self._sampled():
            return self._policy.observe_seconds
        return 0.0

    def record(self, key: str, timing: ContentTiming) -> SampleRef:
        """Add the timing of one usable render of ``key``."""
        profile = self._live(key) or _Profile(self._clock() + self._policy.ttl_seconds)
        sequence = next(self._sequence)
        profile.samples.append((sequence, timing))
        self._store(key, profile)
        return SampleRef(key, sequence)

    def record_late(self, ref: SampleRef, late_growth_seconds: float | None) -> None:
        """Record a finished late observation of the render ``ref``.

        :param late_growth_seconds: when content last grew after the render was returned,
            relative to the start of its readiness wait; ``None`` if nothing grew.
        """
        profile = self._live(ref.key)
        if profile is None:
            log.debug("Profile %s expired before its late observation finished", ref.key)
            return
        profile.observations += 1
        if late_growth_seconds is not None:
            profile.amend(ref.sequence, late_growth_seconds)

    def count(self) -> int:
        """Number of live section profiles, for monitoring."""
        for key in list(self._profiles):
            self._live(key)
        return len(self._profiles)

    def _sampled(self) -> bool:
        return self._chance() < self._policy.sample_rate

    def _live(self, key: str) -> _Profile | None:
        profile = self._profiles.get(key)
        if profile is None:
            return None
        if profile.expires_at <= self._clock():
            del self._profiles[key]
            return None
        return profile

    def _store(self, key: str, profile: _Profile) -> None:
        self._profiles[key] = profile
        self._profiles.move_to_end(key)
        while len(self._profiles) > MAX_PROFILED_SECTIONS:
            self._profiles.popitem(last=False)
