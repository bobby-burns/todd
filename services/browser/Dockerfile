# A real (headful) Chromium on a virtual display.
#  - CDP on :9223 (internal network only) for browser-use
#  - noVNC on :6080 for the dashboard's Live Browser panel (watch + take over for logins/captchas/2FA)
#  - profile persisted in /data/profile, so you sign in to services once
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium xvfb x11vnc novnc websockify fluxbox socat \
      fonts-liberation fonts-noto-color-emoji fonts-noto-cjk ca-certificates dbus-x11 procps \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash browser && mkdir -p /data && chown browser:browser /data
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

USER browser
ENV DISPLAY=:99 SCREEN_WIDTH=1440 SCREEN_HEIGHT=900
EXPOSE 6080 9223
CMD ["/usr/local/bin/start.sh"]
