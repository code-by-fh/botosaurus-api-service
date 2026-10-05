"""Startup rollback and shutdown of the wired components."""

import pytest

from app.runtime import Adapters, start_runtime
from tests.fakes import FakeHttpFetcher, FakeLauncher, make_settings, public_resolver


class FailingSecondLaunch(FakeLauncher):
    """Launches the first Chrome, then fails like a Chrome that does not come up."""

    async def __call__(self, spec):
        if self.launched:
            raise ConnectionError("Chrome did not start")
        return await super().__call__(spec)


def adapters_with(launcher: FakeLauncher, fetcher: FakeHttpFetcher) -> Adapters:
    return Adapters(
        launcher=launcher,
        fetcher_factory=lambda guard, settings: fetcher,
        resolver=public_resolver,
        pause=lambda: 0.0,
    )


@pytest.mark.anyio
async def test_failed_pool_start_stops_started_browsers_and_closes_the_rest():
    launcher = FailingSecondLaunch()
    fetcher = FakeHttpFetcher()

    with pytest.raises(ConnectionError, match="Chrome did not start"):
        await start_runtime(make_settings(MAX_WORKERS="2"), adapters_with(launcher, fetcher))

    assert [browser.stopped for browser in launcher.launched] == [True]
    assert fetcher.closed is True


@pytest.mark.anyio
async def test_close_closes_every_part_and_reraises_the_first_failure():
    launcher = FakeLauncher()
    runtime = await start_runtime(
        make_settings(MAX_WORKERS="1"), adapters_with(launcher, FakeHttpFetcher(fail_close=True))
    )

    with pytest.raises(ConnectionError, match="curl session already gone"):
        await runtime.close()

    assert launcher.launched[0].stopped is True
    with pytest.raises(RuntimeError, match="egress proxy is not started"):
        runtime.egress.url_for(use_proxy=False)
