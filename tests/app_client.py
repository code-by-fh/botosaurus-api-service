"""Builds a TestClient around the real application with fakes at the system boundaries."""

from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import Adapters, start_runtime
from tests.fakes import FakeHttpFetcher, FakeLauncher, make_settings, public_resolver

TEST_PEER = "testclient"
TEST_PEER_PORT = 50000


def build_client(pages: dict | None = None, peer: str = TEST_PEER, **env: str) -> TestClient:
    """Return a client for an app configured by ``env`` (env-var names) on top of test defaults.

    :param peer: address the requests appear to come from (the direct TCP peer).
    """
    adapters = Adapters(
        launcher=FakeLauncher(pages or {}),
        fetcher_factory=lambda guard, settings: FakeHttpFetcher(),
        resolver=public_resolver,
        pause=lambda: 0.0,
    )

    async def runtime_starter(settings):
        return await start_runtime(settings, adapters)

    application = create_app(make_settings(MAX_WORKERS="1", **env), runtime_starter)
    return TestClient(application, client=(peer, TEST_PEER_PORT))
