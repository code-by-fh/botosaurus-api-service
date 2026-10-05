import pytest

import app.browser.late_content as late_content
import app.browser.readiness as readiness


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fast_readiness_polling(monkeypatch):
    monkeypatch.setattr(readiness, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(late_content, "LATE_POLL_INTERVAL_SECONDS", 0)
    # Integration tests run on the real clock; a zero base window lets an idle fake page
    # settle on its second poll instead of sleeping half a second per render.
    monkeypatch.setattr(readiness, "BASE_QUIET_SECONDS", 0)
