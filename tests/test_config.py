import pytest

from app.config import (
    DEFAULT_LOCALE,
    DEFAULT_MAX_WORKERS,
    DEFAULT_TIMEZONE,
    ConfigError,
    load_settings,
)

VALID_KEYS = "key-one, key-two ,,"


def test_defaults_apply_when_only_api_keys_are_set():
    settings = load_settings({"API_KEYS": "key-one"})

    assert settings.browser.worker_count == DEFAULT_MAX_WORKERS
    assert settings.browser.locale == DEFAULT_LOCALE
    assert settings.browser.timezone == DEFAULT_TIMEZONE
    assert settings.browser.headless is False
    assert settings.http_first.enabled is True
    assert settings.vnc_enabled is False
    assert settings.allow_private_targets is False


def test_api_keys_are_split_and_trimmed():
    settings = load_settings({"API_KEYS": VALID_KEYS})

    assert settings.api_keys == ("key-one", "key-two")


def test_legacy_api_key_variable_is_still_accepted():
    settings = load_settings({"API_KEY": "legacy-key"})

    assert settings.api_keys == ("legacy-key",)


def test_missing_api_key_fails_fast():
    with pytest.raises(ConfigError, match="API_KEYS"):
        load_settings({})


def test_home_proxy_takes_precedence_over_proxy():
    settings = load_settings(
        {"API_KEYS": "k", "HOME_PROXY": "http://home:8888", "PROXY": "http://other:1"}
    )

    assert settings.browser.proxy_url == "http://home:8888"


@pytest.mark.parametrize(
    "name, value, message",
    [
        ("HEADLESS", "maybe", "HEADLESS must be a boolean"),
        ("MAX_WORKERS", "two", "MAX_WORKERS must be a number"),
        ("MAX_WORKERS", "0", "MAX_WORKERS must be >= 1"),
        ("MAX_WORKERS", "1.5", "MAX_WORKERS must be an integer"),
        ("MIN_TEXT_COVERAGE", "1.2", "MIN_TEXT_COVERAGE must be <= 1"),
        ("BROWSER_MAX_AGE_SECONDS", "10", "BROWSER_MAX_AGE_SECONDS must be >= 60"),
        ("HOME_PROXY", "ftp://proxy.example", "HOME_PROXY must look like"),
    ],
)
def test_invalid_values_name_the_variable(name, value, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({"API_KEYS": "k", name: value})
