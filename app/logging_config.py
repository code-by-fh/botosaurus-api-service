"""Centralized logging configuration.

Log level is controlled via the ``LOG_LEVEL`` environment variable.
Defaults to ``INFO`` in production; set to ``DEBUG`` for local development.
Timestamps are always UTC in ISO 8601, independent of the container timezone.

At DEBUG level, external libraries (zendriver, asyncio) and the Python
root logger are also activated so proxy connections, CDP messages and
browser lifecycle events appear in the output.
"""

import logging
import os
import sys
import time

# External loggers surfaced only at DEBUG level to avoid noise in production.
_DEBUG_EXTERNAL_LOGGERS = (
    "zendriver",
    "asyncio",
    "curl_cffi",
)


def setup_logging() -> None:
    """Configure the ``render`` logger and, at DEBUG level, external ones.

    Called once at import time so every module that does
    ``logging.getLogger("render.<name>")`` inherits the same
    level and format automatically.

    At DEBUG level the root logger is also configured so that third-party
    libraries (zendriver, asyncio, curl_cffi) emit their messages and important
    events such as browser launch, proxy negotiations and CDP calls become
    visible.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    render = logging.getLogger("render")
    render.setLevel(level)
    # Prevent duplicate handlers on repeated calls (e.g. tests)
    if not render.handlers:
        render.addHandler(handler)

    if level <= logging.DEBUG:
        # At DEBUG, also capture root-level and external library logs so that
        # proxy negotiations, CDP traffic and browser lifecycle events are
        # visible without enabling them in production.
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        if not root.handlers:
            root.addHandler(handler)

        for name in _DEBUG_EXTERNAL_LOGGERS:
            ext = logging.getLogger(name)
            ext.setLevel(logging.DEBUG)
            if not ext.handlers:
                ext.addHandler(handler)


# Auto-configure on first import
setup_logging()
