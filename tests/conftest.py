import pytest

import app.browser.readiness as readiness


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fast_readiness_polling(monkeypatch):
    monkeypatch.setattr(readiness, "POLL_INTERVAL_SECONDS", 0)
