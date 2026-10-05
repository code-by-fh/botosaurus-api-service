"""Target URLs as they may appear in logs.

Callers' URLs can carry credentials in the userinfo and tokens in the query
string (signed links, session ids, API keys of the target). Logs keep what
explains a render, scheme, host and path, and drop the rest.
"""

from urllib.parse import urlsplit, urlunsplit

MASK = "***"


def loggable_url(url: object) -> str:
    """``url`` without userinfo and fragment, with any query replaced by ``***``."""
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    query = MASK if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
