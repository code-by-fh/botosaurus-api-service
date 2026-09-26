FROM nikolaik/python-nodejs:python3.12-nodejs18-slim

# System dependencies: Chrome, Xvfb, VNC, download tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg2 \
    ca-certificates \
    apt-transport-https \
    xvfb \
    x11vnc \
    openbox \
    xdotool \
    x11-utils \
    xdg-utils \
    lsof \
    git \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome stable
RUN wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
       http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# noVNC (web VNC client)
RUN git clone --depth 1 --branch v1.4.0 \
    https://github.com/novnc/noVNC /opt/novnc \
    && git clone --depth 1 \
    https://github.com/novnc/websockify /opt/novnc/utils/websockify \
    && ln -s /opt/novnc/utils/websockify/run /opt/novnc/utils/launch.sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DISPLAY=:99
ENV CHROME_BIN=/usr/bin/google-chrome

EXPOSE 8000 6080

ENTRYPOINT ["/entrypoint.sh"]
