FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ANONYMIZED_TELEMETRY=false \
    BROWSER_USE_CLOUD_SYNC=false \
    TODD_DATA_DIR=/data \
    TODD_PLUGINS_DIR=/app/custom/plugins \
    TODD_PROMPTS_DIR=/app/custom/prompts

# Claude Code CLI (the "Your Claude plan" engine). Unmodified, installed from npm; you sign in once with
# `docker compose exec -it api claude auth login`, and the sign-in lives in the data volume.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code@latest \
    && npm cache clean --force && rm -rf /var/lib/apt/lists/*
ENV CLAUDE_HOME=/data/claude \
    CLAUDE_CONFIG_DIR=/data/claude/.claude \
    DISABLE_AUTOUPDATER=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY todd ./todd
COPY tests ./tests
RUN mkdir -p /data /app/custom/plugins /app/custom/prompts

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "todd.main:app", "--host", "0.0.0.0", "--port", "8000"]
