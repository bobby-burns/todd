"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


class Config:
    data_dir = Path(_env("TODD_DATA_DIR", "./.todd-data"))
    database_url = _env("DATABASE_URL", "")  # empty -> sqlite in data_dir
    secret_key = _env("TODD_SECRET_KEY", "")  # empty -> generated and stored in data_dir

    sandbox_url = _env("SANDBOX_URL", "http://sandbox:7000")
    sandbox_token = _env("SANDBOX_TOKEN", "change-me-sandbox-token")
    workspace_root = _env("SANDBOX_WORKSPACE", "/workspace")

    browser_cdp_host = _env("BROWSER_CDP_HOST", "browser")
    browser_cdp_port = int(_env("BROWSER_CDP_PORT", "9223"))
    browser_live_url = _env(
        "BROWSER_LIVE_URL", "http://localhost:6080/vnc_lite.html?scale=true"
    )

    # Shared secret between web and api. When set, every /api route except /api/health requires it, so other
    # containers (sandbox, browser) can't call the API even though they share a Docker network.
    api_token = _env("TODD_API_TOKEN", "")
    api_token_file = _env("TODD_API_TOKEN_FILE", "")  # generated on first start if missing (shared with web)

    cors_origins = [o.strip() for o in _env("CORS_ORIGINS", "http://localhost:3000").split(",")]

    # Claude Code engine: the CLI binary, where its sign-in lives (a volume), and how its sessions reach this API's
    # MCP endpoint (same container, so loopback).
    claude_bin = _env("CLAUDE_BIN", "claude")
    claude_home = Path(_env("CLAUDE_HOME", str(Path(_env("TODD_DATA_DIR", "./.todd-data")) / "claude")))
    claude_config_dir = Path(_env("CLAUDE_CONFIG_DIR", str(claude_home / ".claude")))
    internal_url = _env("TODD_INTERNAL_URL", "http://127.0.0.1:8000")

    planner_max_turns = int(_env("TODD_PLANNER_MAX_TURNS", "60"))
    code_max_steps = int(_env("TODD_CODE_MAX_STEPS", "80"))
    browser_max_steps = int(_env("TODD_BROWSER_MAX_STEPS", "75"))

    def __init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.claude_config_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not self.api_token and self.api_token_file:
            path = Path(self.api_token_file)
            if not path.exists() or not path.read_text().strip():
                import secrets

                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secrets.token_urlsafe(32))
                os.chmod(path, 0o644)  # readable by the web container's user
            self.api_token = path.read_text().strip()
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.data_dir / 'todd.db').resolve()}"


config = Config()
