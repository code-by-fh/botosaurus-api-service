import json
import logging

import pytest
from fastapi.testclient import TestClient

import app.browser.readiness as readiness
import app.main as main_module
from app.api_models import RenderRequest
from app.main import API_PREFIX
from app.security_headers import SECURITY_HEADERS
from tests.app_client import build_client
from tests.fakes import (
    SECOND_API_KEY,
    SERVER_RENDERED_HTML,
    TEST_API_KEY,
    FakePage,
    restless_probes,
)

RENDER_PATH = f"{API_PREFIX}/render"
TARGET_URL = "https://example.com/article"
AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}
UUID_HEX_LENGTH = 32


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


def test_health_detail_reports_remembered_proxy_host_count(client):
    response = client.get("/health/detail", headers=AUTH)

    assert response.json()["proxy_hosts"] == {"entries": 0}


def test_health_detail_reports_profile_entry_count(client):
    client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=AUTH)

    response = client.get("/health/detail", headers=AUTH)

    assert response.json()["profiles"] == {"entries": 1}
    assert response.json()["pool"]["observing"] == 0


def test_render_returns_html_with_engine_headers(client):
    response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=AUTH)

    assert response.status_code == 200
    assert response.text == SERVER_RENDERED_HTML
    assert response.headers["x-render-engine"] == "browser"
    assert response.headers["x-render-stable"] == "true"
    assert response.headers["x-render-ready-reason"] == "settled"
    assert response.headers["x-render-profile"] == "cold"
    assert response.headers["x-final-url"] == TARGET_URL
    assert response.headers["x-upstream-status"] == "200"
    assert response.headers["x-render-route"] == "direct"


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


@pytest.mark.parametrize(
    ("field", "value"),
    [("wait_for_settle", 1), ("idle_timeout", 5)],
)
def test_removed_tuning_fields_are_rejected_as_unknown(client, field, value):
    body = {"url": TARGET_URL, "wait_for": "#price", field: value}

    response = client.post(RENDER_PATH, json=body, headers=AUTH)

    error = response.json()["error"]
    assert response.status_code == 400
    assert error["code"] == "VALIDATION_ERROR"
    assert [entry["field"] for entry in error["fields"]] == [field]


def test_restless_page_with_wait_for_is_returned_unstable_once_element_is_found(
    caplog, monkeypatch
):
    monkeypatch.setattr(readiness, "WAIT_FOR_QUIET_CAP_SECONDS", 0)
    pages = {TARGET_URL: FakePage(probes=restless_probes())}
    body = {"url": TARGET_URL, "wait_for": "#price"}

    with build_client(pages) as client, caplog.at_level(logging.INFO, logger="render.api"):
        response = client.post(RENDER_PATH, json=body, headers=AUTH)

    [line] = timing_lines(caplog)
    assert response.status_code == 200
    assert response.headers["x-render-stable"] == "false"
    assert response.headers["x-render-ready-reason"] == "wait-for-found"
    assert "readiness_end=wait-for-found" in line


def test_openapi_documents_render_headers_and_error_envelope(client):
    schema = client.app.openapi()

    responses = schema["paths"][RENDER_PATH]["post"]["responses"]
    assert set(responses["200"]["headers"]) == {
        "X-Render-Engine",
        "X-Render-Stable",
        "X-Render-Ready-Reason",
        "X-Render-Profile",
        "X-Render-Route",
        "X-Final-Url",
        "X-Upstream-Status",
        "X-Request-ID",
        *SECURITY_HEADERS,
    }
    assert "Retry-After" in responses["429"]["headers"]
    assert "REQUEST_TOO_LARGE" in responses["413"]["description"]
    assert "RESPONSE_TOO_LARGE" in responses["502"]["description"]
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
    keys = ("queue_ms=", "readiness_ms=", "output_ms=", "total_ms=", "readiness_end=settled")
    for key in (*keys, "quiet_ms=", "inflight_ignored=0", "route=direct", "escalated=false"):
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


@pytest.mark.parametrize(
    ("block_resources", "logged"),
    [
        (False, "blocked=none"),
        (["stylesheet", "font"], "blocked=font,stylesheet"),
        (["image", "font", "media", "stylesheet"], "blocked=font,image,media,stylesheet"),
    ],
)
def test_block_resources_accepts_false_or_list_and_logs_the_kinds(
    client, caplog, block_resources, logged
):
    body = {"url": TARGET_URL, "block_resources": block_resources}

    with caplog.at_level(logging.INFO, logger="render.api"):
        response = client.post(RENDER_PATH, json=body, headers=AUTH)

    [line] = timing_lines(caplog)
    assert response.status_code == 200
    assert logged in line.split()


@pytest.mark.parametrize(
    ("block_resources", "message"),
    [
        ([], "must not be empty"),
        (["image", "image"], "must not repeat 'image'"),
        (["image", "websocket"], "item 1 is not one of image, font, media, stylesheet"),
        (["trackers"], "item 0 is not one of image, font, media, stylesheet"),
        (True, "must be false or a list"),
        ("image", "must be false or a list"),
        (None, "must be false or a list"),
    ],
)
def test_invalid_block_resources_is_one_field_error(client, block_resources, message):
    body = {"url": TARGET_URL, "block_resources": block_resources}

    response = client.post(RENDER_PATH, json=body, headers=AUTH)

    error = response.json()["error"]
    assert response.status_code == 400
    assert [field["field"] for field in error["fields"]] == ["block_resources"]
    assert message in error["fields"][0]["message"]


def test_openapi_documents_block_resources_as_false_or_list_of_kinds(client):
    schema = client.app.openapi()

    field = schema["components"]["schemas"]["RenderRequest"]["properties"]["block_resources"]
    disabled, kinds = field["anyOf"]
    assert disabled == {"type": "boolean", "const": False}
    assert kinds["type"] == "array"
    assert kinds["minItems"] == 1
    assert kinds["uniqueItems"] is True
    kind_schema = kinds["items"]["$ref"].rsplit("/", 1)[1]
    enum = schema["components"]["schemas"][kind_schema]["enum"]
    assert enum == ["image", "font", "media", "stylesheet"]


def test_openapi_request_examples_start_with_the_minimal_request(client):
    schema = client.app.openapi()

    body = schema["paths"][RENDER_PATH]["post"]["requestBody"]["content"]["application/json"]
    examples = [example["value"] for example in body["examples"].values()]
    assert examples[0] == {"url": "https://example.com"}
    assert any({"format", "selector"} <= set(example) for example in examples)
    assert any("wait_for" in example for example in examples)


def test_openapi_documents_only_the_current_request_fields(client):
    schema = client.app.openapi()

    properties = schema["components"]["schemas"]["RenderRequest"]["properties"]
    assert set(properties) == {
        "url",
        "format",
        "selector",
        "wait_for",
        "mode",
        "use_proxy",
        "timeout",
        "block_resources",
    }
    assert schema["components"]["schemas"]["RenderRequest"]["required"] == ["url"]
    for removed in ("wait_for_settle", "idle_timeout", "trackers"):
        assert removed not in json.dumps(schema)


def test_timing_line_masks_credentials_and_query_of_the_target(caplog):
    url = "https://user:secret@example.com/article?token=abc"
    with build_client({url: FakePage()}) as client, caplog.at_level(logging.INFO):
        client.post(RENDER_PATH, json={"url": url}, headers=AUTH)

    [line] = timing_lines(caplog)
    assert "url=https://example.com/article?*** " in line
    assert "secret" not in line
    assert "token" not in line


def test_final_url_header_drops_credentials():
    url = "https://user:secret@example.com/article"
    with build_client({url: FakePage()}) as client:
        response = client.post(RENDER_PATH, json={"url": url}, headers=AUTH)

    assert response.headers["x-final-url"] == TARGET_URL


@pytest.mark.parametrize("trace_id", ["a b", "x=1", 'q"uote', "x" * 65])
def test_unsafe_trace_ids_are_replaced(client, trace_id):
    response = client.get("/health", headers={"X-Request-ID": trace_id})

    assert response.headers["x-request-id"] != trace_id
    assert len(response.headers["x-request-id"]) == UUID_HEX_LENGTH


def test_safe_trace_id_is_kept(client):
    trace_id = "client-1.req:42_A"

    response = client.get("/health", headers={"X-Request-ID": trace_id})

    assert response.headers["x-request-id"] == trace_id


def test_unexpected_errors_carry_security_headers_and_trace_id(monkeypatch):
    def broken_output(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_module, "build_output", broken_output)
    headers = {**AUTH, "X-Request-ID": "trace-500"}
    with TestClient(build_client().app, raise_server_exceptions=False) as client:
        response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=headers)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["traceId"] == "trace-500"
    assert response.headers["x-request-id"] == "trace-500"
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


@pytest.fixture
def restored_render_log_level():
    render = logging.getLogger("render")
    original = render.level
    yield
    render.setLevel(original)


def test_log_level_setting_configures_the_service_logger(restored_render_log_level):
    with build_client(LOG_LEVEL="warning"):
        level = logging.getLogger("render").level

    assert level == logging.WARNING


@pytest.mark.parametrize("field", ["selector", "wait_for"])
@pytest.mark.parametrize(
    ("selector", "message"),
    [
        ("div:has(p:has(a))", "must not nest"),
        ("div:not(:is(.a))", "must not nest"),
        (":is(a):is(b):not(c):where(d):has(e)", "must not use more than 4"),
        ("p::before", "cannot be matched"),
    ],
)
def test_costly_or_unmatchable_selectors_are_field_errors(client, field, selector, message):
    response = client.post(RENDER_PATH, json={"url": TARGET_URL, field: selector}, headers=AUTH)

    [error] = response.json()["error"]["fields"]
    assert response.status_code == 400
    assert error["field"] == field
    assert message in error["message"]


@pytest.mark.parametrize("selector", ['p:-soup-contains("Price")', 'p:contains("Price")'])
def test_wait_for_rejects_soupsieve_only_pseudo_classes(client, selector):
    body = {"url": TARGET_URL, "wait_for": selector}

    response = client.post(RENDER_PATH, json=body, headers=AUTH)

    [error] = response.json()["error"]["fields"]
    assert response.status_code == 400
    assert "Chrome rejects them" in error["message"]


def test_flat_pseudo_classes_and_quoted_lookalikes_are_accepted():
    selector = 'main:not(.ad) a[title=":has(x)"]:is(.link, .button)'

    request = RenderRequest(url=TARGET_URL, selector=selector, wait_for=selector)

    assert (request.selector, request.wait_for) == (selector, selector)


def test_page_larger_than_the_response_cap_is_refused():
    limit = 1024
    huge_page = FakePage(html=f"<html><body>{'x' * limit}</body></html>")
    pages = {TARGET_URL: huge_page}
    with build_client(pages, MAX_RESPONSE_BYTES=str(limit)) as client:
        response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=AUTH)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "RESPONSE_TOO_LARGE"


def test_page_that_breaks_the_size_check_is_still_refused_when_too_large():
    limit = 1024
    huge_page = FakePage(html=f"<html><body>{'x' * limit}</body></html>", size_check_fails=True)
    pages = {TARGET_URL: huge_page}
    with build_client(pages, MAX_RESPONSE_BYTES=str(limit)) as client:
        response = client.post(RENDER_PATH, json={"url": TARGET_URL}, headers=AUTH)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "RESPONSE_TOO_LARGE"
