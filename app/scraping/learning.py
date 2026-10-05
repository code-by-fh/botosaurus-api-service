"""What the service learns from one browser render: readiness timing and late content.

``SectionLearning`` is the ``page_loader.RenderLearning`` hook of one request.
It records the content timing of a usable render into the section profile and,
when the profile store asks for it, has the returned page watched for late
content. Late content is also proof that the returned document was incomplete,
so it marks the section browser-only: an HTTP fetch that matched the early
render would match an incomplete page.

For the same reason a render becomes the reference of an HTTP verification only
after a completed watch saw no growth. A watch that was skipped, cut short or
failed proves nothing, so its render is never verified, and with the watch
disabled no section can earn the HTTP fast path. A section without a verdict is
watched on every render (while no request waits), not only on sampled ones.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.browser.page_loader import BrowserPage
from app.log_safety import loggable_url
from app.scraping.profiles import SampleRef, SectionProfileStore
from app.scraping.verdicts import Verdict, VerdictStore, section_key

log = logging.getLogger("render.profiles")

HTTP_OK = 200

VerificationHook = Callable[[BrowserPage], None]


def is_usable_reference(page: BrowserPage, blocks_resources: bool) -> bool:
    """Whether a browser render may teach anything that is shared by every client.

    A render with blocked resources may lack lazy-loaded content, so HTTP could match
    it while missing text a full render shows, and its timing is not the site's. Only
    a stable render of a page the site served normally counts.
    """
    if blocks_resources:
        return False
    return page.stable and page.status == HTTP_OK


@dataclass(frozen=True)
class LearningStores:
    """Where learned knowledge goes."""

    profiles: SectionProfileStore
    verdicts: VerdictStore


@dataclass(frozen=True)
class LearningSubject:
    """The request whose render is learned from.

    ``verify`` starts the HTTP verification of a render whose watch saw no late
    growth; ``None`` when the request may not use the HTTP fast path.
    """

    url: str
    blocks_resources: bool
    verify: VerificationHook | None = None


class SectionLearning:
    """Learns from the browser render of one request (``page_loader.RenderLearning``)."""

    def __init__(self, stores: LearningStores, subject: LearningSubject):
        self._stores = stores
        self._subject = subject
        self._key = section_key(subject.url)
        self._ref: SampleRef | None = None
        self._page: BrowserPage | None = None
        verdict = stores.verdicts.get(self._key)
        unverified = subject.verify is not None and verdict is Verdict.UNKNOWN
        self.observe_seconds = stores.profiles.observe_seconds_for(self._key, unverified)

    def rendered(self, page: BrowserPage) -> bool:
        """Record the timing of a usable render; ``True`` if it should be watched."""
        if not is_usable_reference(page, self._subject.blocks_resources):
            return False
        self._ref = self._stores.profiles.record(self._key, page.timing)
        self._page = page
        return self.observe_seconds > 0

    def observed(self, late_growth_seconds: float | None) -> None:
        """Record a finished watch; without late growth the render may be verified,
        with it the section becomes browser-only."""
        if self._ref is None:
            return
        self._stores.profiles.record_late(self._ref, late_growth_seconds)
        if late_growth_seconds is None:
            self._verify_clean_render()
            return
        log.info(
            "Content of %s grew %.1fs after the readiness wait began, after it was returned; "
            "section %s now waits longer and stays in the browser",
            loggable_url(self._subject.url),
            late_growth_seconds,
            self._key,
        )
        self._stores.verdicts.record_mismatch(self._key)

    def _verify_clean_render(self) -> None:
        if self._subject.verify is not None and self._page is not None:
            self._subject.verify(self._page)
