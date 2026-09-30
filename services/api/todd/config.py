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
    # Commands that use your sign-ins (git push/pull, gh, deploy CLIs, EAS, CLI sign-ins) run in their own container
    # with the same workspace, so nothing else running in the sandbox can read their tokens. Empty: the sandbox.
    signedin_url = _env("SIGNEDIN_URL", "")
    signedin_token = _env("SIGNEDIN_TOKEN", "")
    signedin_token_file = _env("SIGNEDIN_TOKEN_FILE", "")  # generated on first start if missing (shared with it)

    # The media service (image embeddings, captioned slides for the video toolset): only the API reaches it, with a
    # random token the API makes (media-token volume). It mounts the workspace, so it reads and writes run folders.
    media_url = _env("MEDIA_URL", "http://media:7000")
    media_token = _env("MEDIA_TOKEN", "")
    media_token_file = _env("MEDIA_TOKEN_FILE", "")  # generated on first start if missing (shared with it)

    browser_cdp_host = _env("BROWSER_CDP_HOST", "browser")
    browser_cdp_port = int(_env("BROWSER_CDP_PORT", "9223"))
    browser_live_url = _env(
        "BROWSER_LIVE_URL", "http://localhost:6080/vnc_lite.html?scale=true"
    )
    # The live view's password (the browser container makes a random one unless VNC_PASSWORD is set)
    browser_live_password = _env("VNC_PASSWORD", "")
    browser_live_password_file = _env("VNC_PASSWORD_FILE", "")

    def live_url(self) -> str:
        """The dashboard's live-view URL, with the password so the frame connects without asking."""
        from urllib.parse import quote

        pw = self.browser_live_password
        if not pw and self.browser_live_password_file:
            try:
                pw = Path(self.browser_live_password_file).read_text().strip()
            except OSError:
                pw = ""
        if not pw or "password=" in self.browser_live_url:
            return self.browser_live_url
        sep = "&" if "?" in self.browser_live_url else "?"
        return f"{self.browser_live_url}{sep}password={quote(pw)}"

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
            self.api_token = _token_file(Path(self.api_token_file))
        if self.signedin_url and not self.signedin_token and self.signedin_token_file:
            self.signedin_token = _token_file(Path(self.signedin_token_file))
        if self.media_url and not self.media_token and self.media_token_file:
            self.media_token = _token_file(Path(self.media_token_file))
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.data_dir / 'todd.db').resolve()}"


def _token_file(path: Path) -> str:
    """A random token kept in a file (made on first start), readable by the container it's shared with."""
    if not path.exists() or not path.read_text().strip():
        import secrets

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32))
        os.chmod(path, 0o644)  # readable by the other container's user
    return path.read_text().strip()


config = Config()
