import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
import app.main as main_module
from app.renderer import NavigationError, RenderTimeoutError


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    pool.acquire.return_value = MagicMock()
    pool.busy = 1
    pool.total = 3
    return pool


@pytest.fixture
def client(mock_pool):
    with patch("app.main.BrowserPool", return_value=mock_pool):
        with TestClient(app) as c:
            yield c, mock_pool


def test_render_returns_html(client):
    c, _ = client
    with patch("app.main.render", return_value="<html>hello</html>"):
        resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text == "<html>hello</html>"


def test_render_503_when_pool_exhausted(client, mock_pool):
    c, pool = client
    pool.acquire.return_value = None
    resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "pool_exhausted"


def test_render_422_on_missing_url(client):
    c, _ = client
    resp = c.post("/render", json={})
    assert resp.status_code == 422


def test_render_502_on_navigation_error(client):
    c, _ = client
    with patch("app.main.render", side_effect=NavigationError("failed")):
        resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "navigation_failed"


def test_render_504_on_timeout(client):
    c, _ = client
    with patch("app.main.render", side_effect=RenderTimeoutError("timed out")):
        resp = c.post("/render", json={"url": "https://example.com"})
    assert resp.status_code == 504
    assert resp.json()["detail"]["error"] == "timeout"


def test_render_releases_driver_on_error(client, mock_pool):
    c, pool = client
    with patch("app.main.render", side_effect=NavigationError("oops")):
        c.post("/render", json={"url": "https://example.com"})
    pool.release.assert_called_once()


def test_health_returns_pool_status(client, mock_pool):
    c, pool = client
    pool.busy = 2
    pool.total = 3
    resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["workers_total"] == 3


def test_vnc_returns_html_with_iframe(client):
    c, _ = client
    resp = c.get("/vnc")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "iframe" in resp.text
    assert "6080" in resp.text
