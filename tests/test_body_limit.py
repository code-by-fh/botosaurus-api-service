"""Oversized request bodies are refused before parsing and authentication."""

import json

from app.body_limit import MAX_REQUEST_BODY_BYTES
from app.main import API_PREFIX
from app.security_headers import SECURITY_HEADERS
from tests.app_client import build_client
from tests.fakes import TEST_API_KEY

RENDER_PATH = f"{API_PREFIX}/render"
AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}
JSON_CONTENT = {"Content-Type": "application/json"}
CHUNK_BYTES = 8 * 1024


def oversized_body() -> bytes:
    padding = "a" * MAX_REQUEST_BODY_BYTES
    return json.dumps({"url": f"https://example.com/?{padding}"}).encode()


def chunks(body: bytes):
    for start in range(0, len(body), CHUNK_BYTES):
        yield body[start : start + CHUNK_BYTES]


def test_declared_oversized_body_is_refused_without_authentication():
    with build_client() as client:
        response = client.post(RENDER_PATH, content=oversized_body(), headers=JSON_CONTENT)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


def test_streamed_oversized_body_without_length_is_refused():
    headers = {**AUTH, **JSON_CONTENT}
    with build_client() as client:
        response = client.post(RENDER_PATH, content=chunks(oversized_body()), headers=headers)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


def test_refusal_carries_trace_id_and_security_headers():
    headers = {**JSON_CONTENT, "X-Request-ID": "trace-413"}
    with build_client() as client:
        response = client.post(RENDER_PATH, content=oversized_body(), headers=headers)

    assert response.json()["error"]["traceId"] == "trace-413"
    assert response.headers["x-request-id"] == "trace-413"
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_streamed_body_within_the_limit_reaches_the_application():
    body = json.dumps({"url": "https://example.com/article"}).encode()
    headers = {**AUTH, **JSON_CONTENT}
    with build_client() as client:
        response = client.post(RENDER_PATH, content=chunks(body), headers=headers)

    assert response.status_code == 200


def test_body_at_the_limit_is_parsed_normally():
    body = b" " * (MAX_REQUEST_BODY_BYTES - 2) + b"{}"
    headers = {**AUTH, **JSON_CONTENT}
    with build_client() as client:
        response = client.post(RENDER_PATH, content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
