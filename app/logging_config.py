"""Centralized logging configuration.

The level comes from the validated ``LOG_LEVEL`` setting (see ``app.config``), so
a typo stops the service at startup instead of silently logging at another level.
Timestamps are always UTC in ISO 8601, independent of the container timezone.

At DEBUG level, external libraries (zendriver, asyncio) and the Python
root logger are also activated so proxy connections, CDP messages and
browser lifecycle events appear in the output.
"""

import logging
import sys
import time

# External loggers surfaced only at DEBUG level to avoid noise in production.
_DEBUG_EXTERNAL_LOGGERS = (
    "zendriver",
    "asyncio",
    "curl_cffi",
)
LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def setup_logging(level_name: str) -> None:
    """Configure the ``render`` logger and, at DEBUG level, external ones.

    Called once by ``create_app`` so every module that does
    ``logging.getLogger("render.<name>")`` inherits the same level and format.
    Repeated calls (tests) change the level but add no second handler.

    :param level_name: a validated level name (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``).
    """
    level = logging.getLevelNamesMapping()[level_name]
    handler = _stdout_handler()
    _attach(logging.getLogger("render"), level, handler)
    if level <= logging.DEBUG:
        # Proxy negotiations, CDP traffic and browser lifecycle events become visible
        # only when asked for; in production they are noise.
        _attach(logging.getLogger(), logging.DEBUG, handler)
        for name in _DEBUG_EXTERNAL_LOGGERS:
            _attach(logging.getLogger(name), logging.DEBUG, handler)


def _stdout_handler() -> logging.Handler:
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=UTC_TIMESTAMP_FORMAT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    return handler


def _attach(logger: logging.Logger, level: int, handler: logging.Handler) -> None:
    logger.setLevel(level)
    if not logger.handlers:
        logger.addHandler(handler)
