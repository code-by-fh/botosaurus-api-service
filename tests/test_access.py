"""Access control around the API: documentation, security headers and the auth lockout."""

import pytest

from app.auth_throttle import MAX_AUTH_FAILURES
from app.main import API_PREFIX
from app.security_headers import SECURITY_HEADERS
from tests.app_client import build_client
from tests.fakes import TEST_API_KEY

DOCS_PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]
BASIC_AUTH = ("any-user", TEST_API_KEY)
WRONG_BEARER = {"Authorization": "Bearer wrong-key"}
VALID_BEARER = {"Authorization": f"Bearer {TEST_API_KEY}"}
HEALTH_DETAIL = "/health/detail"
PROXY_ADDRESS = "10.0.0.2"
FIRST_CLIENT = "203.0.113.7"
SECOND_CLIENT = "203.0.113.8"


def forwarded_for(client_ip: str, key_header: dict) -> dict:
    return {**key_header, "X-Forwarded-For": client_ip}


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_are_not_served_by_default(path):
    with build_client() as client:
        response = client.get(path, auth=BASIC_AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_enabled_docs_require_basic_auth(path):
    with build_client(ENABLE_DOCS="true") as client:
        response = client.get(path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_enabled_docs_reject_wrong_password(path):
    with build_client(ENABLE_DOCS="true") as client:
        response = client.get(path, auth=("any-user", "wrong-key"))

    assert response.status_code == 401


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_enabled_docs_are_served_with_an_api_key(path):
    with build_client(ENABLE_DOCS="true") as client:
        response = client.get(path, auth=BASIC_AUTH)

    assert response.status_code == 200


def test_swagger_ui_loads_the_guarded_schema():
    with build_client(ENABLE_DOCS="true") as client:
        page = client.get("/docs", auth=BASIC_AUTH).text
        schema = client.get("/openapi.json", auth=BASIC_AUTH).json()

    assert "/openapi.json" in page
    assert f"{API_PREFIX}/render" in schema["paths"]


@pytest.mark.parametrize("path", ["/health", "/does-not-exist"])
def test_every_response_carries_the_security_headers(path):
    with build_client() as client:
        response = client.get(path)

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_frame_policy_allows_same_origin_framing_only():
    with build_client() as client:
        response = client.get("/health")

    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"


def test_repeated_wrong_keys_lock_the_client_out():
    with build_client() as client:
        for _ in range(MAX_AUTH_FAILURES):
            client.get(HEALTH_DETAIL, headers=WRONG_BEARER)
        response = client.get(HEALTH_DETAIL, headers=WRONG_BEARER)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "TOO_MANY_AUTH_FAILURES"
    assert int(response.headers["retry-after"]) > 0


def test_locked_out_client_is_refused_even_with_a_valid_key():
    with build_client() as client:
        for _ in range(MAX_AUTH_FAILURES):
            client.get(HEALTH_DETAIL, headers=WRONG_BEARER)
        response = client.get(HEALTH_DETAIL, headers=VALID_BEARER)

    assert response.status_code == 429


def test_failures_below_the_limit_do_not_lock_out():
    with build_client() as client:
        for _ in range(MAX_AUTH_FAILURES - 1):
            client.get(HEALTH_DETAIL, headers=WRONG_BEARER)
        response = client.get(HEALTH_DETAIL, headers=VALID_BEARER)

    assert response.status_code == 200


def test_missing_credentials_do_not_count_as_failures():
    with build_client() as client:
        for _ in range(MAX_AUTH_FAILURES):
            client.get(HEALTH_DETAIL)
        response = client.get(HEALTH_DETAIL, headers=VALID_BEARER)

    assert response.status_code == 200


def test_wrong_basic_passwords_count_towards_the_lockout():
    with build_client(ENABLE_DOCS="true") as client:
        for _ in range(MAX_AUTH_FAILURES):
            client.get("/docs", auth=("any-user", "wrong-key"))
        response = client.get("/docs", auth=BASIC_AUTH)

    assert response.status_code == 429


def test_forwarded_for_is_ignored_from_untrusted_peers():
    with build_client() as client:
        for index in range(MAX_AUTH_FAILURES):
            client.get(HEALTH_DETAIL, headers=forwarded_for(f"198.51.100.{index}", WRONG_BEARER))
        response = client.get(HEALTH_DETAIL, headers=forwarded_for(SECOND_CLIENT, VALID_BEARER))

    assert response.status_code == 429


def test_behind_a_trusted_proxy_the_lockout_is_per_forwarded_client():
    with build_client(peer=PROXY_ADDRESS, TRUSTED_PROXY_IPS=PROXY_ADDRESS) as client:
        for _ in range(MAX_AUTH_FAILURES):
            client.get(HEALTH_DETAIL, headers=forwarded_for(FIRST_CLIENT, WRONG_BEARER))
        locked = client.get(HEALTH_DETAIL, headers=forwarded_for(FIRST_CLIENT, VALID_BEARER))
        other = client.get(HEALTH_DETAIL, headers=forwarded_for(SECOND_CLIENT, VALID_BEARER))

    assert locked.status_code == 429
    assert other.status_code == 200


@pytest.mark.parametrize(
    "authorization",
    ["Basic", "Basic !!!not-base64!!!", "Basic " + "bm8tY29sb24=", "Digest abc"],
)
def test_malformed_basic_headers_get_the_login_challenge(authorization):
    with build_client(ENABLE_DOCS="true") as client:
        response = client.get("/docs", headers={"Authorization": authorization})

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")
