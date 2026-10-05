import pytest

from app.config import (
    DEFAULT_AUTO_PROXY_CHALLENGE_SECONDS,
    DEFAULT_AUTO_PROXY_TTL_SECONDS,
    DEFAULT_CLEARANCE_MAX_AGE_SECONDS,
    DEFAULT_LATE_CONTENT_OBSERVE_SECONDS,
    DEFAULT_LOCALE,
    DEFAULT_MAX_WORKERS,
    DEFAULT_PROFILE_OBSERVE_SAMPLE_RATE,
    DEFAULT_PROFILE_TTL_SECONDS,
    DEFAULT_TIMEZONE,
    DEFAULT_VERDICT_SAME_PAGE_INTERVAL_SECONDS,
    DEFAULT_VNC_PORT,
    MIN_API_KEY_LENGTH,
    ConfigError,
    load_settings,
)

KEY_ONE = "key-one-0123456789abcdefghijklmnop"
KEY_TWO = "key-two-0123456789abcdefghijklmnop"
VALID_KEYS = f"{KEY_ONE}, {KEY_TWO} ,,"


def test_defaults_apply_when_only_api_keys_are_set():
    settings = load_settings({"API_KEYS": KEY_ONE})

    assert settings.browser.worker_count == DEFAULT_MAX_WORKERS
    assert settings.browser.locale == DEFAULT_LOCALE
    assert settings.browser.timezone == DEFAULT_TIMEZONE
    assert settings.browser.headless is False
    assert settings.http_first.enabled is True
    assert settings.access.vnc_enabled is False
    assert settings.access.docs_enabled is False
    assert settings.access.vnc_port == DEFAULT_VNC_PORT
    assert settings.access.trusted_proxies == ()
    assert settings.allow_private_targets is False
    assert settings.clearance.enabled is True
    assert settings.clearance.max_age_seconds == DEFAULT_CLEARANCE_MAX_AGE_SECONDS


def test_clearance_reuse_can_be_disabled_and_its_max_age_changed():
    settings = load_settings(
        {"API_KEYS": KEY_ONE, "CLEARANCE_REUSE": "false", "CLEARANCE_MAX_AGE_SECONDS": "120"}
    )

    assert settings.clearance.enabled is False
    assert settings.clearance.max_age_seconds == 120


def test_profile_learning_defaults():
    settings = load_settings({"API_KEYS": KEY_ONE})

    assert settings.profiles.observe_seconds == DEFAULT_LATE_CONTENT_OBSERVE_SECONDS
    assert settings.profiles.sample_rate == DEFAULT_PROFILE_OBSERVE_SAMPLE_RATE
    assert settings.profiles.ttl_seconds == DEFAULT_PROFILE_TTL_SECONDS


def test_auto_proxy_defaults():
    settings = load_settings({"API_KEYS": KEY_ONE})

    assert settings.auto_proxy.enabled is True
    assert settings.auto_proxy.challenge_seconds == DEFAULT_AUTO_PROXY_CHALLENGE_SECONDS
    assert settings.auto_proxy.ttl_seconds == DEFAULT_AUTO_PROXY_TTL_SECONDS


def test_auto_proxy_can_be_disabled_and_tuned():
    env = {
        "API_KEYS": KEY_ONE,
        "AUTO_PROXY_ON_BLOCK": "false",
        "AUTO_PROXY_CHALLENGE_SECONDS": "5",
        "AUTO_PROXY_TTL_SECONDS": "3600",
    }

    settings = load_settings(env)

    assert settings.auto_proxy.enabled is False
    assert settings.auto_proxy.challenge_seconds == 5
    assert settings.auto_proxy.ttl_seconds == 3600


def test_late_content_observation_can_be_disabled():
    settings = load_settings({"API_KEYS": KEY_ONE, "LATE_CONTENT_OBSERVE_SECONDS": "0"})

    assert settings.profiles.observe_seconds == 0


def test_same_page_verdict_interval_has_a_default_and_can_be_changed():
    default = load_settings({"API_KEYS": KEY_ONE})
    changed = load_settings({"API_KEYS": KEY_ONE, "VERDICT_SAME_PAGE_INTERVAL_SECONDS": "900"})

    assert default.http_first.same_page_interval_seconds == (
        DEFAULT_VERDICT_SAME_PAGE_INTERVAL_SECONDS
    )
    assert changed.http_first.same_page_interval_seconds == 900


def test_same_page_verdict_interval_must_be_shorter_than_the_verdict_lifetime():
    env = {
        "API_KEYS": KEY_ONE,
        "VERDICT_TTL_SECONDS": "600",
        "VERDICT_SAME_PAGE_INTERVAL_SECONDS": "600",
    }

    with pytest.raises(ConfigError, match="must be < VERDICT_TTL_SECONDS"):
        load_settings(env)


def test_trusted_proxies_accept_addresses_and_ranges():
    settings = load_settings({"API_KEYS": KEY_ONE, "TRUSTED_PROXY_IPS": "10.0.0.2, 172.16.0.0/12"})

    assert [str(network) for network in settings.access.trusted_proxies] == [
        "10.0.0.2/32",
        "172.16.0.0/12",
    ]


def test_api_keys_are_split_and_trimmed():
    settings = load_settings({"API_KEYS": VALID_KEYS})

    assert settings.api_keys == (KEY_ONE, KEY_TWO)


def test_legacy_api_key_variable_is_still_accepted():
    settings = load_settings({"API_KEY": KEY_ONE})

    assert settings.api_keys == (KEY_ONE,)


def test_missing_api_key_fails_fast():
    with pytest.raises(ConfigError, match="API_KEYS"):
        load_settings({})


def test_home_proxy_takes_precedence_over_proxy():
    settings = load_settings(
        {"API_KEYS": KEY_ONE, "HOME_PROXY": "http://home:8888", "PROXY": "http://other:1"}
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
        ("HOME_PROXY", "http://proxy.example", "HOME_PROXY must look like"),
        ("HOME_PROXY", "http://proxy.example:port", "HOME_PROXY must look like"),
        ("HOME_PROXY", "http://proxy.example:0", "HOME_PROXY must look like"),
        ("HOME_PROXY", "socks5://proxy.example:65536", "HOME_PROXY must look like"),
        ("HOME_PROXY", "http://:8888", "HOME_PROXY must look like"),
        ("MIN_TEXT_COVERAGE", "nan", "MIN_TEXT_COVERAGE must be a finite number"),
        ("QUEUE_TIMEOUT_SECONDS", "inf", "QUEUE_TIMEOUT_SECONDS must be a finite number"),
        ("PROFILE_OBSERVE_SAMPLE_RATE", "NaN", "PROFILE_OBSERVE_SAMPLE_RATE must be a finite"),
        ("BROWSER_MAX_AGE_SECONDS", "Infinity", "BROWSER_MAX_AGE_SECONDS must be a finite"),
        ("MAX_WORKERS", "-inf", "MAX_WORKERS must be a finite number"),
        ("LOG_LEVEL", "VERBOSE", "LOG_LEVEL must be one of"),
        ("ENABLE_DOCS", "sometimes", "ENABLE_DOCS must be a boolean"),
        ("TRUSTED_PROXY_IPS", "proxy.example", "TRUSTED_PROXY_IPS must list IP addresses"),
        ("VNC_PORT", "70000", "VNC_PORT must be <= 65535"),
        ("CLEARANCE_REUSE", "perhaps", "CLEARANCE_REUSE must be a boolean"),
        ("CLEARANCE_MAX_AGE_SECONDS", "0", "CLEARANCE_MAX_AGE_SECONDS must be >= 1"),
        ("CLEARANCE_MAX_AGE_SECONDS", "3601", "CLEARANCE_MAX_AGE_SECONDS must be <= 3600"),
        ("LATE_CONTENT_OBSERVE_SECONDS", "-1", "LATE_CONTENT_OBSERVE_SECONDS must be >= 0"),
        ("LATE_CONTENT_OBSERVE_SECONDS", "31", "LATE_CONTENT_OBSERVE_SECONDS must be <= 30"),
        ("PROFILE_OBSERVE_SAMPLE_RATE", "1.5", "PROFILE_OBSERVE_SAMPLE_RATE must be <= 1"),
        ("PROFILE_OBSERVE_SAMPLE_RATE", "-0.1", "PROFILE_OBSERVE_SAMPLE_RATE must be >= 0"),
        ("PROFILE_TTL_SECONDS", "59", "PROFILE_TTL_SECONDS must be >= 60"),
        ("PROFILE_TTL_SECONDS", "604801", "PROFILE_TTL_SECONDS must be <= 604800"),
        ("AUTO_PROXY_ON_BLOCK", "maybe", "AUTO_PROXY_ON_BLOCK must be a boolean"),
        ("AUTO_PROXY_CHALLENGE_SECONDS", "1", "AUTO_PROXY_CHALLENGE_SECONDS must be >= 2"),
        ("AUTO_PROXY_CHALLENGE_SECONDS", "61", "AUTO_PROXY_CHALLENGE_SECONDS must be <= 60"),
        ("AUTO_PROXY_TTL_SECONDS", "59", "AUTO_PROXY_TTL_SECONDS must be >= 60"),
        ("AUTO_PROXY_TTL_SECONDS", "604801", "AUTO_PROXY_TTL_SECONDS must be <= 604800"),
        (
            "VERDICT_SAME_PAGE_INTERVAL_SECONDS",
            "59",
            "VERDICT_SAME_PAGE_INTERVAL_SECONDS must be >= 60",
        ),
    ],
)
def test_invalid_values_name_the_variable(name, value, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({"API_KEYS": KEY_ONE, name: value})


def test_log_level_is_validated_case_insensitively():
    settings = load_settings({"API_KEYS": KEY_ONE, "LOG_LEVEL": "debug"})

    assert settings.log_level == "DEBUG"


def test_log_level_defaults_to_info():
    assert load_settings({"API_KEYS": KEY_ONE}).log_level == "INFO"


def test_home_proxy_with_credentials_and_port_is_accepted():
    proxy = "socks5://user:secret@proxy.example:1080"

    settings = load_settings({"API_KEYS": KEY_ONE, "HOME_PROXY": proxy})

    assert settings.browser.proxy_url == proxy


def test_placeholder_api_key_is_rejected():
    placeholder = "CHANGE_ME_APP_ONE_0123456789abcdefghij"

    with pytest.raises(ConfigError, match="API key #2 is still the CHANGE_ME placeholder"):
        load_settings({"API_KEYS": f"{KEY_ONE},{placeholder}"})


def test_short_api_key_is_rejected_without_revealing_it():
    short_key = "a" * (MIN_API_KEY_LENGTH - 1)

    with pytest.raises(ConfigError, match="API key #1 is shorter than 32 characters") as error:
        load_settings({"API_KEYS": short_key})

    assert short_key not in str(error.value)


def test_api_key_of_minimum_length_is_accepted():
    key = "a" * MIN_API_KEY_LENGTH

    assert load_settings({"API_KEYS": key}).api_keys == (key,)


def test_vnc_requires_headed_chrome():
    env = {"API_KEYS": KEY_ONE, "ENABLE_VNC": "true", "HEADLESS": "true"}

    with pytest.raises(ConfigError, match="ENABLE_VNC=true requires HEADLESS=false"):
        load_settings(env)
