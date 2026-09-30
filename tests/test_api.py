import logging

import pytest

from app.api_models import MAX_WAIT_FOR_SETTLE_SECONDS
from app.main import API_PREFIX
from app.security_headers import SECURITY_HEADERS
from tests.app_client import build_client
from tests.fakes import (
    SECOND_API_KEY,
    SERVER_RENDERED_HTML,
    TEST_API_KEY,
    FakePage,
    stable_probe,
)

RENDER_PATH = f"{API_PREFIX}/render"
TARGET_URL = "https://example.com/article"
AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}


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


def test_health_detail_reports_clearance_entry_count_only(client):
    response = client.get("/health/detail", headers=AUTH)

    assert response.json()["clearance"] == {"enabled": True, "entries": 0}


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


def test_wait_for_settle_requires_wait_for(client):
    body = {"url": TARGET_URL, "wait_for_settle": 1}

    response = client.post(RENDER_PATH, json=body, headers=AUTH)

    assert response.status_code == 400
    assert "wait_for_settle requires wait_for" in response.json()["error"]["fields"][0]["message"]


@pytest.mark.parametrize("settle", [-0.5, MAX_WAIT_FOR_SETTLE_SECONDS + 0.5])
def test_wait_for_settle_outside_range_is_rejected(client, settle):
    body = {"url": TARGET_URL, "wait_for": "#price", "wait_for_settle": settle}

    response = client.post(RENDER_PATH, json=body, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["fields"][0]["field"] == "wait_for_settle"


def test_zero_wait_for_settle_returns_page_on_first_usable_poll(caplog):
    restless = [stable_probe(text_length=length) for length in range(1, 100_000)]
    pages = {TARGET_URL: FakePage(probes=restless)}
    body = {"url": TARGET_URL, "wait_for": "#price", "wait_for_settle": 0}

    with build_client(pages) as client, caplog.at_level(logging.INFO, logger="render.api"):
        response = client.post(RENDER_PATH, json=body, headers=AUTH)

    [line] = timing_lines(caplog)
    assert response.status_code == 200
    assert response.headers["x-render-stable"] == "false"
    assert "readiness_end=wait-for-found" in line


def test_openapi_documents_render_headers_and_error_envelope(client):
    schema = client.app.openapi()

    responses = schema["paths"][RENDER_PATH]["post"]["responses"]
    assert set(responses["200"]["headers"]) == {
        "X-Render-Engine",
        "X-Render-Stable",
        "X-Final-Url",
        "X-Upstream-Status",
        "X-Request-ID",
        *SECURITY_HEADERS,
    }
    assert "Retry-After" in responses["429"]["headers"]
    assert responses["400"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "ErrorResponse"
    )
    assert "422" not in responses


def test_openapi_documents_auth_lockout_on_protected_routes(client):
    schema = client.app.openapi()

    responses = schema["paths"]["/health/detail"]["get"]["responses"]
    assert "TOO_MANY_AUTH_FAILURES" in responses["429"]["description"]
    assert "Retry-After" in responses["429"]["headers"]


def timing_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Render timing ")
    ]


def test_successful_render_logs_one_timing_line_with_trace_id(client, caplog):
    headers = {**AUTH, "X-Request-ID": "trace-timing"}

    with caplog.at_level(logging.INFO, logger="render.api"):
        client.post(RENDER_PATH, json={"url": TARGET_URL, "format": "markdown"}, headers=headers)

    [line] = timing_lines(caplog)
    assert line.startswith(
        f"Render timing traceId=trace-timing url={TARGET_URL} outcome=ok engine=browser "
    )
    for key in ("queue_ms=", "readiness_ms=", "output_ms=", "total_ms=", "readiness_end=settled"):
        assert key in line


def test_failed_render_logs_timing_line_with_error_code(caplog):
    unreachable = FakePage(navigation_error="net::ERR_NAME_NOT_RESOLVED")

    with build_client(pages={TARGET_URL: unreachable}) as client, caplog.at_level(logging.INFO):
        response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=AUTH)

    [line] = timing_lines(caplog)
    assert response.status_code == 502
    assert "outcome=NAVIGATION_FAILED engine=none" in line
    assert "navigate_ms=" in line
    assert "readiness_ms=" not in line
