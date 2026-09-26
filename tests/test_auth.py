"""Tests for app.auth – API-key authentication."""

import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasicCredentials

from app.auth import verify_api_key, verify_basic_auth, _get_api_key, reset_cached_key


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure the cached key is reset between tests."""
    reset_cached_key()
    yield
    reset_cached_key()


# --- Bearer token tests ---

def test_verify_accepts_valid_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-secret-key-abc")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-secret-key-abc")
    # Should not raise
    verify_api_key(creds)


def test_verify_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "correct-key")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-key")
    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(creds)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "unauthorized"


def test_verify_rejects_empty_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "correct-key")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="")
    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(creds)
    assert exc_info.value.status_code == 401


# --- Basic auth tests ---

def test_basic_auth_accepts_valid_password(monkeypatch):
    monkeypatch.setenv("API_KEY", "my-secret")
    creds = HTTPBasicCredentials(username="anyone", password="my-secret")
    # Should not raise
    verify_basic_auth(creds)


def test_basic_auth_ignores_username(monkeypatch):
    monkeypatch.setenv("API_KEY", "my-secret")
    for username in ["", "admin", "user@example.com", "root"]:
        reset_cached_key()
        creds = HTTPBasicCredentials(username=username, password="my-secret")
        verify_basic_auth(creds)  # Should not raise


def test_basic_auth_rejects_wrong_password(monkeypatch):
    monkeypatch.setenv("API_KEY", "correct-password")
    creds = HTTPBasicCredentials(username="admin", password="wrong-password")
    with pytest.raises(HTTPException) as exc_info:
        verify_basic_auth(creds)
    assert exc_info.value.status_code == 401
    assert "WWW-Authenticate" in exc_info.value.headers


def test_basic_auth_rejects_empty_password(monkeypatch):
    monkeypatch.setenv("API_KEY", "correct-password")
    creds = HTTPBasicCredentials(username="admin", password="")
    with pytest.raises(HTTPException) as exc_info:
        verify_basic_auth(creds)
    assert exc_info.value.status_code == 401


# --- Shared key management tests ---

def test_missing_api_key_env_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API_KEY environment variable is not set"):
        _get_api_key()


def test_empty_api_key_env_raises_runtime_error(monkeypatch):
    monkeypatch.setenv("API_KEY", "   ")
    with pytest.raises(RuntimeError, match="API_KEY environment variable is not set"):
        _get_api_key()


def test_key_is_cached_after_first_call(monkeypatch):
    monkeypatch.setenv("API_KEY", "cached-key")
    key1 = _get_api_key()
    # Change the env var – should still return the cached value
    monkeypatch.setenv("API_KEY", "different-key")
    key2 = _get_api_key()
    assert key1 == key2 == "cached-key"

