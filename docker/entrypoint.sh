#!/bin/bash
# Starts the virtual display (headed mode only), the optional noVNC debug view
# and the API server.
set -euo pipefail

DISPLAY_NUMBER=99
SCREEN_GEOMETRY=1920x1080x24
VNC_RFB_PORT=5900
# websockify listens on loopback only; the API relays it under /vnc.
NOVNC_PORT="${VNC_PORT:-6080}"
NOVNC_WEB_ROOT=/usr/share/novnc
DISPLAY_WAIT_ATTEMPTS=50

is_true() {
  case "${1,,}" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for_display() {
  for _ in $(seq "$DISPLAY_WAIT_ATTEMPTS"); do
    [[ -S "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ]] && return 0
    sleep 0.1
  done
  echo "[entrypoint] Xvfb did not come up" >&2
  exit 1
}

start_vnc() {
  if [[ -z "${VNC_PASSWORD:-}" ]]; then
    echo "[entrypoint] ENABLE_VNC=true requires VNC_PASSWORD" >&2
    exit 1
  fi
  local password_file
  password_file="$(mktemp)"
  x11vnc -storepasswd "$VNC_PASSWORD" "$password_file" >/dev/null
  echo "[entrypoint] Starting x11vnc and noVNC on 127.0.0.1:${NOVNC_PORT} (served via /vnc)"
  x11vnc -display ":${DISPLAY_NUMBER}" -forever -shared -localhost -quiet \
    -rfbauth "$password_file" -rfbport "$VNC_RFB_PORT" &
  websockify --web "$NOVNC_WEB_ROOT" "127.0.0.1:${NOVNC_PORT}" "127.0.0.1:${VNC_RFB_PORT}" &
}

if ! is_true "${HEADLESS:-false}"; then
  echo "[entrypoint] Starting Xvfb on :${DISPLAY_NUMBER} (${SCREEN_GEOMETRY})"
  Xvfb ":${DISPLAY_NUMBER}" -screen 0 "$SCREEN_GEOMETRY" -nolisten tcp &
  wait_for_display
  openbox &
  if is_true "${ENABLE_VNC:-false}"; then
    start_vnc
  fi
fi

echo "[entrypoint] Starting API on :8000"
exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 \
  --timeout-graceful-shutdown 30
