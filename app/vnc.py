"""HTML page that embeds the noVNC live view of the headed browsers."""

import html
import os

DEFAULT_VNC_PORT = "6080"


def _viewer_url(host: str, scheme: str) -> str:
    prefix = os.environ.get("NOVNC_PREFIX", "").strip()
    if prefix:
        clean_prefix = "/" + prefix.strip("/")
        ws_path = clean_prefix.lstrip("/") + "/websockify"
        return f"{clean_prefix}/vnc.html?autoconnect=true&resize=scale&path={ws_path}"
    port = os.environ.get("VNC_PORT", DEFAULT_VNC_PORT)
    return f"{scheme}://{host}:{port}/vnc.html?autoconnect=true&resize=scale&path=websockify"


def vnc_page(host: str, scheme: str) -> str:
    """Return the viewer page; ``NOVNC_PREFIX`` switches to reverse-proxy paths."""
    src = html.escape(_viewer_url(host, scheme), quote=True)
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Browser View - noVNC</title>
  <style>
    body, html {{ margin: 0; padding: 0; height: 100%; background: #1a1a1a; }}
    iframe {{ width: 100%; height: 100%; border: none; display: block; }}
  </style>
</head>
<body>
  <iframe src="{src}"></iframe>
</body>
</html>"""
