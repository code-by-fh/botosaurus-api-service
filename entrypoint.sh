#!/bin/bash
set -e

echo "[entrypoint] Starting Xvfb on display :99"
Xvfb :99 -screen 0 1280x720x24 -ac &
XVFB_PID=$!
sleep 1

echo "[entrypoint] Starting openbox window manager"
openbox &
sleep 0.5

echo "[entrypoint] Starting x11vnc on :5900"
x11vnc -display :99 -forever -shared -nopw -noxdamage -rfbport 5900 &
sleep 0.5

echo "[entrypoint] Starting noVNC websockify on :6080"
websockify --web /opt/novnc 6080 127.0.0.1:5900 &
sleep 0.5

echo "[entrypoint] Starting FastAPI on :8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
