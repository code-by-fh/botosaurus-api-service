"""Centralized logging configuration.

Log level is controlled via the ``LOG_LEVEL`` environment variable.
Defaults to ``INFO`` in production; set to ``DEBUG`` for local development.
Timestamps are always UTC in ISO 8601, independent of the container timezone.
"""

import logging
import os
import sys
import time


def setup_logging() -> None:
    """Configure the root ``botosaurus`` logger.

    Called once at import time so every module that does
    ``logging.getLogger("botosaurus.<name>")`` inherits the same
    level and format automatically.
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

    root = logging.getLogger("botosaurus")
    root.setLevel(level)
    # Prevent duplicate handlers on repeated calls (e.g. tests)
    if not root.handlers:
        root.addHandler(handler)


# Auto-configure on first import
setup_logging()
