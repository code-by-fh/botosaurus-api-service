# syntax=docker/dockerfile:1
FROM python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ARG DEBIAN_FRONTEND=noninteractive
ARG APP_UID=10001

# Virtual display + optional VNC debug view, and fonts so that the font
# fingerprint looks like a desktop rather than a bare server.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg tini \
      xvfb openbox x11vnc novnc websockify \
      fonts-liberation fonts-dejavu-core fonts-noto-color-emoji fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Real Google Chrome, not Debian's Chromium: Chromium lacks proprietary codecs,
# which contradicts a "Chrome" user agent.
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid "${APP_UID}" --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint
COPY --chmod=0755 docker/chrome-launcher.sh /usr/local/bin/chrome-launcher

ENV DISPLAY=:99 \
    CHROME_BIN=/usr/local/bin/chrome-launcher \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint"]
