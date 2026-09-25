# The sandbox is where code agents work: node 22 + pnpm + git + python + common deploy CLIs.
# Only the API can reach it (no published ports). It never sees the vault — tokens are injected per command.
FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq unzip zip ripgrep python3 python3-venv python3-pip build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable && npm install -g vercel firebase-tools && npm cache clean --force

RUN python3 -m venv /opt/sandbox && /opt/sandbox/bin/pip install --no-cache-dir \
      fastapi==0.141.1 "uvicorn[standard]==0.54.0"

RUN useradd -m -s /bin/bash agent && mkdir -p /workspace && chown agent:agent /workspace
COPY server.py /opt/sandbox/server.py

USER agent
WORKDIR /workspace
ENV WORKSPACE_ROOT=/workspace NEXT_TELEMETRY_DISABLED=1 CI=1
EXPOSE 7000
CMD ["/opt/sandbox/bin/uvicorn", "--app-dir", "/opt/sandbox", "server:app", "--host", "0.0.0.0", "--port", "7000"]
