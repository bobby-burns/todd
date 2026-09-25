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

VNC_ARGS=(-display :99 -forever -shared -rfbport 5900 -quiet -noxdamage)
if [[ -n "${VNC_PASSWORD:-}" ]]; then
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
  VNC_ARGS+=(-rfbauth /tmp/vncpass)
else
  VNC_ARGS+=(-nopw)
fi
x11vnc "${VNC_ARGS[@]}" &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

# Chrome only binds CDP to localhost; expose it to the compose network via socat.
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &

# Keep Chromium running even if a human closes the window in the live view.
while true; do
  "${CHROME_BIN:-chromium}" \
    --user-data-dir="$PROFILE" \
    --remote-debugging-port=9222 \
    --remote-allow-origins='*' \
    --no-first-run --no-default-browser-check --hide-crash-restore-bubble \
    --disable-dev-shm-usage --no-sandbox --test-type \
    --password-store=basic \
    --window-position=0,0 --window-size="${W},${H}" --start-maximized \
    about:blank || true
  echo "chromium exited; restarting in 1s" >&2
  sleep 1
done
