"""Service configuration, read once from environment variables at startup.

Every setting has a documented default (see README). Invalid values raise
``ConfigError`` so the service fails fast instead of running misconfigured.
"""

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_MAX_WORKERS = 2
DEFAULT_BROWSER_MAX_PAGES = 150
DEFAULT_BROWSER_MAX_AGE_SECONDS = 45 * 60
DEFAULT_QUEUE_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_QUEUE_SIZE = 8
DEFAULT_MAX_CONCURRENCY_PER_HOST = 2
DEFAULT_VERDICT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_VERDICT_MIN_SAMPLES = 2
DEFAULT_MIN_TEXT_COVERAGE = 0.9
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
DEFAULT_LOCALE = "de-DE"
DEFAULT_TIMEZONE = "Europe/Berlin"
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
FALSY_VALUES = frozenset({"0", "false", "no", "off", ""})
PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
DEFAULT_VNC_PORT = 6080
# Below the 300 s immunity that challenge vendors typically grant a solved
# token, so a stored token is dropped before the vendor stops accepting it.
DEFAULT_CLEARANCE_MAX_AGE_SECONDS = 240.0
MIN_CLEARANCE_MAX_AGE_SECONDS = 1
# Tokens older than an hour are almost always expired or re-validated by the
# vendor; allowing more would only keep stale tokens around.
MAX_CLEARANCE_MAX_AGE_SECONDS = 3600
MAX_TCP_PORT = 65535

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class ConfigError(ValueError):
    """Raised when an environment variable is missing or has an invalid value."""


@dataclass(frozen=True)
class BrowserSettings:
    """Settings for launching and recycling Chrome instances."""

    worker_count: int
    headless: bool
    sandbox: bool
    executable_path: str | None
    locale: str
    timezone: str
    proxy_url: str | None
    max_pages: int
    max_age_seconds: float


@dataclass(frozen=True)
class QueueSettings:
    """Settings for admission control in front of the browser pool."""

    timeout_seconds: float
    max_waiting: int
    max_per_host: int


@dataclass(frozen=True)
class HttpFirstSettings:
    """Settings for the browserless HTTP fast path and its verification."""

    enabled: bool
    verdict_ttl_seconds: float
    verdict_min_samples: int
    min_text_coverage: float
    max_response_bytes: int


@dataclass(frozen=True)
class AccessSettings:
    """Which interactive endpoints are exposed and whom to trust for client addresses."""

    docs_enabled: bool
    vnc_enabled: bool
    vnc_port: int
    trusted_proxies: tuple[IpNetwork, ...]


@dataclass(frozen=True)
class ClearanceSettings:
    """Reuse of anti-bot clearance cookies between browser renders."""

    enabled: bool
    max_age_seconds: float


@dataclass(frozen=True)
class Settings:
    """Complete, validated service configuration."""

    api_keys: tuple[str, ...]
    browser: BrowserSettings
    queue: QueueSettings
    http_first: HttpFirstSettings
    allow_private_targets: bool
    access: AccessSettings
    clearance: ClearanceSettings


class _EnvReader:
    """Typed accessors over an environment mapping; every error names the variable."""

    def __init__(self, env: Mapping[str, str]):
        self._env = env

    def text(self, name: str, default: str | None = None) -> str | None:
        value = self._env.get(name, "").strip()
        return value or default

    def flag(self, name: str, default: bool) -> bool:
        raw = self._env.get(name)
        if raw is None:
            return default
        normalized = raw.strip().lower()
        if normalized in TRUTHY_VALUES:
            return True
        if normalized in FALSY_VALUES:
            return False
        raise ConfigError(f"{name} must be a boolean, got {raw!r}")

    def number(self, name: str, default: float, minimum: float) -> float:
        raw = self._env.get(name, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
        if value < minimum:
            raise ConfigError(f"{name} must be >= {minimum}, got {value}")
        return value

    def integer(self, name: str, default: int, minimum: int) -> int:
        value = self.number(name, default, minimum)
        if not value.is_integer():
            raise ConfigError(f"{name} must be an integer, got {value}")
        return int(value)


def _read_api_keys(reader: _EnvReader) -> tuple[str, ...]:
    raw = reader.text("API_KEYS") or reader.text("API_KEY") or ""
    keys = tuple(key.strip() for key in raw.split(",") if key.strip())
    if not keys:
        raise ConfigError(
            "API_KEYS (or API_KEY) is not set. The service cannot start without an API key."
        )
    return keys


def _read_proxy(reader: _EnvReader) -> str | None:
    proxy_url = reader.text("HOME_PROXY") or reader.text("PROXY")
    if proxy_url is None:
        return None
    parts = urlsplit(proxy_url)
    if parts.scheme.lower() not in PROXY_SCHEMES or not parts.hostname:
        raise ConfigError("HOME_PROXY must look like http(s)://[user:pass@]host:port or socks5://")
    return proxy_url


def _read_browser(reader: _EnvReader) -> BrowserSettings:
    return BrowserSettings(
        worker_count=reader.integer("MAX_WORKERS", DEFAULT_MAX_WORKERS, 1),
        headless=reader.flag("HEADLESS", False),
        sandbox=reader.flag("BROWSER_SANDBOX", False),
        executable_path=reader.text("CHROME_BIN"),
        locale=reader.text("BROWSER_LOCALE", DEFAULT_LOCALE),
        timezone=reader.text("BROWSER_TIMEZONE", DEFAULT_TIMEZONE),
        proxy_url=_read_proxy(reader),
        max_pages=reader.integer("BROWSER_MAX_PAGES", DEFAULT_BROWSER_MAX_PAGES, 1),
        max_age_seconds=reader.number(
            "BROWSER_MAX_AGE_SECONDS", DEFAULT_BROWSER_MAX_AGE_SECONDS, 60
        ),
    )


def _read_queue(reader: _EnvReader) -> QueueSettings:
    return QueueSettings(
        timeout_seconds=reader.number("QUEUE_TIMEOUT_SECONDS", DEFAULT_QUEUE_TIMEOUT_SECONDS, 0),
        max_waiting=reader.integer("MAX_QUEUE_SIZE", DEFAULT_MAX_QUEUE_SIZE, 0),
        max_per_host=reader.integer(
            "MAX_CONCURRENCY_PER_HOST", DEFAULT_MAX_CONCURRENCY_PER_HOST, 1
        ),
    )


def _read_http_first(reader: _EnvReader) -> HttpFirstSettings:
    coverage = reader.number("MIN_TEXT_COVERAGE", DEFAULT_MIN_TEXT_COVERAGE, 0)
    if coverage > 1:
        raise ConfigError(f"MIN_TEXT_COVERAGE must be <= 1, got {coverage}")
    return HttpFirstSettings(
        enabled=reader.flag("HTTP_FIRST_ENABLED", True),
        verdict_ttl_seconds=reader.number("VERDICT_TTL_SECONDS", DEFAULT_VERDICT_TTL_SECONDS, 60),
        verdict_min_samples=reader.integer("VERDICT_MIN_SAMPLES", DEFAULT_VERDICT_MIN_SAMPLES, 1),
        min_text_coverage=coverage,
        max_response_bytes=reader.integer("MAX_RESPONSE_BYTES", DEFAULT_MAX_RESPONSE_BYTES, 1024),
    )


def _read_trusted_proxies(reader: _EnvReader) -> tuple[IpNetwork, ...]:
    raw = reader.text("TRUSTED_PROXY_IPS") or ""
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    try:
        return tuple(ipaddress.ip_network(entry, strict=False) for entry in entries)
    except ValueError as exc:
        message = f"TRUSTED_PROXY_IPS must list IP addresses or CIDR ranges: {exc}"
        raise ConfigError(message) from exc


def _read_access(reader: _EnvReader) -> AccessSettings:
    vnc_port = reader.integer("VNC_PORT", DEFAULT_VNC_PORT, 1)
    if vnc_port > MAX_TCP_PORT:
        raise ConfigError(f"VNC_PORT must be <= {MAX_TCP_PORT}, got {vnc_port}")
    return AccessSettings(
        docs_enabled=reader.flag("ENABLE_DOCS", False),
        vnc_enabled=reader.flag("ENABLE_VNC", False),
        vnc_port=vnc_port,
        trusted_proxies=_read_trusted_proxies(reader),
    )


def _read_clearance(reader: _EnvReader) -> ClearanceSettings:
    max_age = reader.number(
        "CLEARANCE_MAX_AGE_SECONDS",
        DEFAULT_CLEARANCE_MAX_AGE_SECONDS,
        MIN_CLEARANCE_MAX_AGE_SECONDS,
    )
    if max_age > MAX_CLEARANCE_MAX_AGE_SECONDS:
        raise ConfigError(
            f"CLEARANCE_MAX_AGE_SECONDS must be <= {MAX_CLEARANCE_MAX_AGE_SECONDS}, got {max_age}"
        )
    return ClearanceSettings(enabled=reader.flag("CLEARANCE_REUSE", True), max_age_seconds=max_age)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build validated settings from ``env`` (defaults to ``os.environ``).

    :raises ConfigError: if a required variable is missing or any value is invalid.
    """
    reader = _EnvReader(os.environ if env is None else env)
    return Settings(
        api_keys=_read_api_keys(reader),
        browser=_read_browser(reader),
        queue=_read_queue(reader),
        http_first=_read_http_first(reader),
        allow_private_targets=reader.flag("ALLOW_PRIVATE_TARGETS", False),
        access=_read_access(reader),
        clearance=_read_clearance(reader),
    )
