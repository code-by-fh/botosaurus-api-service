import pytest
from fastapi.testclient import TestClient

from app.main import API_PREFIX, create_app
from app.runtime import Adapters, start_runtime
from tests.fakes import (
    SECOND_API_KEY,
    SERVER_RENDERED_HTML,
    TEST_API_KEY,
    FakeHttpFetcher,
    FakeLauncher,
    FakePage,
    make_settings,
    public_resolver,
    stable_probe,
)

RENDER_PATH = f"{API_PREFIX}/render"
TARGET_URL = "https://example.com/article"
AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}


def build_client(
    pages: dict | None = None, follow_redirects: bool = True, **env: str
) -> TestClient:
    adapters = Adapters(
        launcher=FakeLauncher(pages or {}),
        fetcher_factory=lambda guard, settings: FakeHttpFetcher(),
        resolver=public_resolver,
        pause=lambda: 0.0,
    )

    async def runtime_starter(settings):
        return await start_runtime(settings, adapters)

    return TestClient(
        create_app(make_settings(MAX_WORKERS="1", **env), runtime_starter),
        follow_redirects=follow_redirects,
    )


@pytest.fixture
def client():
    with build_client() as test_client:
        yield test_client


def test_health_is_public(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_detail_requires_api_key(client):
    response = client.get("/health/detail")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_health_detail_reports_pool(client):
    response = client.get("/health/detail", headers=AUTH)

    assert response.json()["pool"]["total"] == 1


def test_render_returns_html_with_engine_headers(client):
    response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=AUTH)

    assert response.status_code == 200
    assert response.text == SERVER_RENDERED_HTML
    assert response.headers["x-render-engine"] == "browser"
    assert response.headers["x-render-stable"] == "true"
    assert response.headers["x-final-url"] == TARGET_URL
    assert response.headers["x-upstream-status"] == "200"


def test_non_ascii_final_url_is_percent_encoded():
    url = "https://example.com/stra\u00dfe"
    with build_client() as client:
        response = client.post(RENDER_PATH, json={"url": url}, headers=AUTH)

    assert response.headers["x-final-url"] == "https://example.com/stra%C3%9Fe"


def test_every_configured_api_key_is_accepted(client):
    headers = {"Authorization": f"Bearer {SECOND_API_KEY}"}

    response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=headers)

    assert response.status_code == 200


def test_unknown_api_key_is_rejected(client):
    headers = {"Authorization": "Bearer wrong-key"}

    response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=headers)

    assert response.status_code == 401


def test_render_markdown_of_selected_element(client):
    body = {"url": TARGET_URL, "selector": "h1", "format": "markdown"}

    response = client.post(RENDER_PATH, json=body, headers=AUTH)

    assert response.text == "# Headline"
    assert response.headers["content-type"].startswith("text/markdown")


def test_element_alias_is_accepted_for_selector(client):
    body = {"url": TARGET_URL, "element": "h1", "format": "markdown"}

    response = client.post(RENDER_PATH, json=body, headers=AUTH)

    assert response.text == "# Headline"


def test_validation_errors_list_each_field(client):
    body = {"url": "not a url", "timeout": 999, "selector": "div[[["}

    response = client.post(RENDER_PATH, json=body, headers=AUTH)

    error = response.json()["error"]
    assert response.status_code == 400
    assert error["code"] == "VALIDATION_ERROR"
    assert {field["field"] for field in error["fields"]} == {"url", "timeout", "selector"}


def test_unknown_fields_are_rejected(client):
    response = client.post(RENDER_PATH, json={"url": TARGET_URL, "headless": True}, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["fields"][0]["field"] == "headless"


def test_errors_carry_the_callers_trace_id(client):
    headers = {**AUTH, "X-Request-ID": "trace-123"}

    response = client.post(
        RENDER_PATH, json={"url": TARGET_URL, "use_proxy": True}, headers=headers
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "PROXY_NOT_CONFIGURED",
        "message": "use_proxy is true, but HOME_PROXY is not set",
        "traceId": "trace-123",
    }
    assert response.headers["x-request-id"] == "trace-123"


def test_navigation_failure_maps_to_bad_gateway():
    pages = {TARGET_URL: FakePage(navigation_error="net::ERR_CONNECTION_REFUSED")}
    with build_client(pages) as client:
        response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=AUTH)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "NAVIGATION_FAILED"


def test_unversioned_render_route_no_longer_exists(client):
    response = client.post("/render", json={"url": TARGET_URL}, headers=AUTH)

    assert response.status_code == 404


def test_vnc_is_hidden_when_disabled(client):
    response = client.get("/vnc", auth=("any", TEST_API_KEY))

    assert response.status_code == 404


def test_vnc_requires_basic_auth():
    with build_client(ENABLE_VNC="true") as client:
        response = client.get("/vnc")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_vnc_page_embeds_viewer_when_enabled():
    with build_client(ENABLE_VNC="true") as client:
        response = client.get("/vnc", auth=("any", TEST_API_KEY))

    assert response.status_code == 200
    assert "vnc.html?autoconnect=true" in response.text


def test_vnc_redirects_when_novnc_prefix_set():
    with build_client(ENABLE_VNC="true", NOVNC_PREFIX="/novnc", follow_redirects=False) as client:
        response = client.get("/vnc", auth=("any", TEST_API_KEY), follow_redirects=False)

    assert response.status_code == 307
    expected = "/novnc/vnc.html?autoconnect=true&resize=scale&path=novnc/websockify"
    assert response.headers["location"] == expected


def test_idle_timeout_requires_wait_for(client):
    response = client.post(RENDER_PATH, json={"url": TARGET_URL, "idle_timeout": 5}, headers=AUTH)

    assert response.status_code == 400
    assert "idle_timeout requires wait_for" in response.json()["error"]["fields"][0]["message"]


def test_idle_timeout_ends_wait_for_early_on_idle_page():
    missing = {TARGET_URL: FakePage(probes=[stable_probe(found=False)])}
    body = {"url": TARGET_URL, "wait_for": "#price", "timeout": 60, "idle_timeout": 1}
    with build_client(missing) as client:
        response = client.post(RENDER_PATH, json=body, headers=AUTH)

    assert response.status_code == 504
    assert "stayed idle for 1s" in response.json()["error"]["message"]


def test_openapi_documents_render_headers_and_error_envelope(client):
    schema = client.get("/openapi.json").json()

    responses = schema["paths"][RENDER_PATH]["post"]["responses"]
    assert set(responses["200"]["headers"]) == {
        "X-Render-Engine",
        "X-Render-Stable",
        "X-Final-Url",
        "X-Upstream-Status",
        "X-Request-ID",
    }
    assert "Retry-After" in responses["429"]["headers"]
    assert responses["400"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "ErrorResponse"
    )
    assert "422" not in responses
