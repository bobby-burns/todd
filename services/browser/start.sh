#!/usr/bin/env bash
set -euo pipefail

W=${SCREEN_WIDTH:-1440}
H=${SCREEN_HEIGHT:-900}
PROFILE=/data/profile
export DISPLAY=${DISPLAY:-:99}
mkdir -p "$PROFILE"
rm -f "$PROFILE"/Singleton* /tmp/.X99-lock  # stale locks after a container restart

Xvfb :99 -screen 0 "${W}x${H}x24" -nolisten tcp &
sleep 1
fluxbox >/dev/null 2>&1 &

# The live view always has a password: without one, any website you visit could connect to localhost:6080 (WebSockets
# aren't limited to the page's own site) and use this browser, where you're signed in to everything. Unless you set
# VNC_PASSWORD, a random one is made once and shared with the API, which puts it in the dashboard's live-view URL.
PASS_FILE=${VNC_PASSWORD_FILE:-/secret/vnc_password}
if [[ -z "${VNC_PASSWORD:-}" ]]; then
  if [[ ! -s "$PASS_FILE" ]]; then
    mkdir -p "$(dirname "$PASS_FILE")"
    (umask 022 && tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 8 > "$PASS_FILE")
  fi
  VNC_PASSWORD=$(cat "$PASS_FILE")
fi
x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
VNC_ARGS=(-display :99 -forever -shared -rfbport 5900 -localhost -quiet -noxdamage -rfbauth /tmp/vncpass)
x11vnc "${VNC_ARGS[@]}" &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

# Chrome only binds CDP to localhost; expose it to the compose network via socat.
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &

# Previews: http://localhost:PORT here reaches the same port in the sandbox, relayed by the API (the sandbox isn't on
# this network). Agents look at what they're building without deploying it. Same ports and base as api and sandbox.
PREVIEW_PORTS=${PREVIEW_PORTS:-3000,3001,4173,4321,5000,5173,8000,8080,8081,19006}
i=0
for port in ${PREVIEW_PORTS//,/ }; do
  socat TCP-LISTEN:"$port",bind=127.0.0.1,fork,reuseaddr TCP:"${PREVIEW_RELAY_HOST:-api}":$((${PREVIEW_RELAY_BASE:-17000} + i)) &
  i=$((i + 1))
done

# Keep Chromium running even if a human closes the window in the live view. No --remote-allow-origins: Todd's CDP
# clients send no Origin, and a web page (which always does) must not be able to open the debugging socket.
while true; do
  "${CHROME_BIN:-chromium}" \
    --user-data-dir="$PROFILE" \
    --remote-debugging-port=9222 \
    --no-first-run --no-default-browser-check --hide-crash-restore-bubble \
    --disable-dev-shm-usage --no-sandbox --test-type \
    --password-store=basic \
    --window-position=0,0 --window-size="${W},${H}" --start-maximized \
    about:blank || true
  echo "chromium exited; restarting in 1s" >&2
  sleep 1
done
