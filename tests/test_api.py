import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app, _env_bool
import app.main as main_module
from app.auth import reset_cached_key
from app.renderer import NavigationError, RenderTimeoutError

TEST_API_KEY = "test-key-for-unit-tests"
AUTH_HEADER = {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("", False),
])
def test_env_bool_parses_truthy_values(value, expected, monkeypatch):
    monkeypatch.setenv("HEADLESS", value)
    assert _env_bool("HEADLESS", False) is expected


def test_env_bool_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("HEADLESS", raising=False)
    assert _env_bool("HEADLESS", False) is False
    assert _env_bool("HEADLESS", True) is True


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    pool.acquire.return_value = MagicMock()
    pool.busy = 1
    pool.total = 3
    return pool


@pytest.fixture
def client(mock_pool, monkeypatch):
    monkeypatch.setenv("API_KEY", TEST_API_KEY)
    reset_cached_key()
    with patch("app.main.BrowserPool", return_value=mock_pool):
        with TestClient(app) as c:
            yield c, mock_pool
    reset_cached_key()


def test_render_returns_html(client):
    c, _ = client
    with patch("app.main.render", return_value="<html>hello</html>"):
        resp = c.post("/render", json={"url": "https://example.com"}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text == "<html>hello</html>"


def test_render_401_without_api_key(client):
    c, _ = client
    resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 401  # Missing Authorization header


def test_render_401_with_wrong_api_key(client):
    c, _ = client
    resp = c.post(
        "/render",
        json={"url": "https://example.com"},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401


def test_render_503_when_pool_exhausted(client, mock_pool):
    c, pool = client
    pool.acquire.return_value = None
    resp = c.post("/render", json={"url": "https://example.com"}, headers=AUTH_HEADER)
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "pool_exhausted"


def test_render_422_on_missing_url(client):
    c, _ = client
    resp = c.post("/render", json={}, headers=AUTH_HEADER)
    assert resp.status_code == 422


def test_render_502_on_navigation_error(client):
    c, _ = client
    with patch("app.main.render", side_effect=NavigationError("failed")):
        resp = c.post("/render", json={"url": "https://example.com"}, headers=AUTH_HEADER)
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "navigation_failed"


def test_render_504_on_timeout(client):
    c, _ = client
    with patch("app.main.render", side_effect=RenderTimeoutError("timed out")):
        resp = c.post("/render", json={"url": "https://example.com"}, headers=AUTH_HEADER)
    assert resp.status_code == 504
    assert resp.json()["detail"]["error"] == "timeout"


def test_render_releases_driver_on_error(client, mock_pool):
    c, pool = client
    with patch("app.main.render", side_effect=NavigationError("oops")):
        c.post("/render", json={"url": "https://example.com"}, headers=AUTH_HEADER)
    pool.release.assert_called_once()


def test_health_returns_ok_without_details(client):
    """Public /health returns only status, no pool info."""
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok"}
    assert "workers_busy" not in body
    assert "workers_total" not in body


def test_health_requires_no_auth(client):
    """Health endpoint must be accessible without authentication."""
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200


def test_health_detail_returns_pool_status(client, mock_pool):
    c, pool = client
    pool.busy = 2
    pool.total = 3
    resp = c.get("/health/detail", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["workers_busy"] == 2
    assert body["workers_total"] == 3


def test_health_detail_requires_auth(client):
    c, _ = client
    resp = c.get("/health/detail")
    assert resp.status_code == 401


def test_vnc_returns_html_with_iframe(client, monkeypatch):
    monkeypatch.setenv("VNC_PORT", "6081")
    c, _ = client
    resp = c.get("/vnc", auth=("admin", TEST_API_KEY))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "iframe" in resp.text
    assert "6081" in resp.text


def test_vnc_requires_auth(client):
    c, _ = client
    resp = c.get("/vnc")
    assert resp.status_code == 401


def test_vnc_rejects_wrong_password(client):
    c, _ = client
    resp = c.get("/vnc", auth=("admin", "wrong-password"))
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") is not None
