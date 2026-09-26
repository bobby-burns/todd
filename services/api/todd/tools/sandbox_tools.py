"""The `sandbox` toolset: a Linux box (node 22, pnpm, git, gh, python, vercel + firebase CLIs) with a workspace
shared by every agent in the run, plus an authenticated git_push and GitHub CLI (`gh`)."""

from __future__ import annotations

import base64
import posixpath
import re
import shlex

from .. import vault
from ..config import config
from ..sdk import ToolError, get_ctx, todd_tool
from . import sandbox

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
# Refuse repo-level config that could redirect a push or run code with the token in the environment.
_GIT_GUARD = ("bad=$(git config --local --get-regexp "
              "'^(url\\..*|credential\\..*|core\\.sshcommand|core\\.hookspath|core\\.askpass|core\\.fsmonitor|http\\..*|"
              "include\\..*|includeif\\..*)$' 2>/dev/null); "
              "if [ -n \"$bad\" ]; then echo \"refusing to push: suspicious git config: $bad\"; exit 3; fi")
NO_TOKEN = ("GITHUB_TOKEN is not configured. If the browser is signed in to GitHub, connect it with "
            "cli_login(\"github\"); otherwise ask the human to add it in Settings → Integrations.")


def _root() -> str:
    return f"{config.workspace_root}/{get_ctx().run_id}"


def _abs(path: str) -> str:
    root = _root()
    p = posixpath.normpath(posixpath.join(root, path or "."))
    if p != root and not p.startswith(root + "/"):
        raise ToolError("path escapes the project directory")
    return p


@todd_tool(toolset="sandbox")
async def shell(cmd: str, timeout_s: int = 300, env: dict[str, str] | None = None) -> dict:
    """Run a bash command in the project directory. Returns exit_code and combined stdout/stderr. Prefer CLIs and
    APIs (npx vercel, npx stripe, firebase, npx wrangler, curl) over the browser; pass credentials through `env`
    as {{secret:NAME}} so they're never in the command text.

    Args:
        cmd: bash command (non-interactive)
        timeout_s: timeout in seconds (default 300, max 1800)
        env: extra environment variables; values may be {{secret:NAME}} (e.g. {"STRIPE_API_KEY": "{{secret:STRIPE_SECRET_KEY}}"})
    """
    from .infra import resolve_secrets

    resolved = {k: resolve_secrets(str(v)) for k, v in (env or {}).items()}
    return await sandbox.exec_(cmd, cwd=_root(), timeout=int(min(max(timeout_s, 5), 1800)), env=resolved)


@todd_tool(toolset="sandbox")
async def write_file(path: str, content: str) -> str:
    """Create or overwrite a file.

    Args:
        path: path relative to the project root
        content: full file content
    """
    await sandbox.write(_abs(path), content)
    return f"wrote {path} ({len(content)} chars)"


@todd_tool(toolset="sandbox")
async def read_file(path: str) -> str:
    """Read a file.

    Args:
        path: path relative to the project root
    """
    return (await sandbox.read(_abs(path)))["content"]


@todd_tool(toolset="sandbox")
async def list_files(path: str = ".", depth: int = 2) -> dict:
    """List files and directories (skips node_modules, .git, .next).

    Args:
        path: directory relative to the project root (default .)
        depth: how many levels deep (default 2)
    """
    return await sandbox.listdir(_abs(path), depth=int(depth))


@todd_tool(toolset="sandbox")
async def git_push(repo: str, message: str = "Update from Todd", branch: str = "main", force: bool = False) -> dict:
    """Commit all changes and push to a GitHub repository. Handles authentication.

    Args:
        repo: GitHub repo as owner/name
        message: commit message
        branch: branch to push (default main)
        force: force push (default false)
    """
    if not _REPO.fullmatch(repo) or ".." in repo:
        raise ToolError("repo must look like owner/name")
    if not _BRANCH.fullmatch(branch):
        raise ToolError("invalid branch name")
    token = vault.get_secret("GITHUB_TOKEN")
    if not token:
        raise ToolError(NO_TOKEN)
    q = shlex.quote
    guard = _GIT_GUARD
    remote = f"https://github.com/{repo}.git"
    script = " && ".join([
        "set -o pipefail",
        "(git rev-parse --is-inside-work-tree >/dev/null 2>&1 || git init -q -b " + q(branch) + ")",
        guard,
        "(git config user.name >/dev/null || git config user.name 'Todd Agent')",
        "(git config user.email >/dev/null || git config user.email 'todd@localhost')",
        "git add -A",
        f"(git diff --cached --quiet || git commit -q --no-verify -m {q(message)})",
        f"git branch -M {q(branch)}",
        # Token goes in as an HTTP header via env-only config; hooks and credential helpers are disabled.
        "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.https://github.com/.extraheader "
        "GIT_CONFIG_VALUE_0=\"Authorization: Basic $(printf 'x-access-token:%s' \"$GIT_TOKEN\" | base64 -w0)\"",
        f"git -c core.hooksPath=/dev/null -c credential.helper= push {'-f ' if force else ''}{q(remote)} "
        f"HEAD:{q(branch)} 2>&1",
        "git log --oneline -1",
    ])
    r = await sandbox.exec_(script, cwd=_root(), timeout=300, env={"GIT_TOKEN": token})
    out = r.get("output") or ""
    for v in (token, base64.b64encode(f"x-access-token:{token}".encode()).decode()):
        out = out.replace(v, "***")
    return {"exit_code": r.get("exit_code"), "output": out}



# Subcommands that could leak the token, run arbitrary code or change how gh authenticates.
_GH_BLOCKED = {"auth", "extension", "extensions", "ext", "alias", "config", "codespace", "cs", "attestation"}


@todd_tool(toolset="sandbox")
async def gh(command: str, timeout_s: int = 300) -> dict:
    """Run the GitHub CLI in the project directory, signed in with the vault's GITHUB_TOKEN (connect it first with
    cli_login("github") if find_integrations says so). Examples: `repo create my-app --private --source . --push`,
    `repo view owner/name`, `pr create --fill`, `release create v1.0 --generate-notes`, `api user`.

    Args:
        command: everything after `gh`, e.g. "repo create my-app --private --source . --push"
        timeout_s: timeout in seconds (default 300)
    """
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ToolError(f"couldn't parse the arguments: {e}") from e
    if not argv or argv[0] in _GH_BLOCKED or any(a == "--hostname" or a.startswith("--hostname=") for a in argv):
        raise ToolError(f"`gh {argv[0] if argv else ''}` isn't available here (blocked: {', '.join(sorted(_GH_BLOCKED))}, "
                        "--hostname). Todd manages GitHub sign-in: use cli_login(\"github\").")
    token = vault.get_secret("GITHUB_TOKEN")
    if not token:
        raise ToolError(NO_TOKEN)
    # git operations inside gh (e.g. `repo create --push`) authenticate through gh for github.com only; hooks off.
    script = " && ".join([
        f"(! git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {{ {_GIT_GUARD}; }})",
        # plain settings go in the script: env values are redacted from the output, and only the token should be
        "export GH_HOST=github.com GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1",
        "export GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null "
        "GIT_CONFIG_KEY_1=credential.https://github.com.helper GIT_CONFIG_VALUE_1= "
        "GIT_CONFIG_KEY_2=credential.https://github.com.helper GIT_CONFIG_VALUE_2='!gh auth git-credential'",
        shlex.join(["gh", *argv]) + " 2>&1",
    ])
    r = await sandbox.exec_(script, cwd=_root(), timeout=int(min(max(timeout_s, 5), 1800)),
                            env={"GH_TOKEN": token})
    return {"exit_code": r.get("exit_code"), "output": (r.get("output") or "").replace(token, "***")}


@todd_tool(toolset="sandbox")
async def cli(service: str, command: str, path: str = ".", timeout_s: int = 600) -> dict:
    """Run a service's CLI signed in with the human's account (connected once with cli_login, or on the Accounts
    page), in a project directory. No tokens or login steps needed. Examples: cli("vercel", "deploy --prod --yes"),
    cli("netlify", "deploy --prod --dir dist"), cli("cloudflare", "pages deploy dist --project-name site"),
    cli("railway", "up --detach"), cli("firebase", "deploy --only hosting"), cli("stripe", "products list").
    For GitHub use `gh`.

    Args:
        service: vercel, netlify, railway, cloudflare (wrangler), stripe or firebase
        command: arguments for the CLI, e.g. "deploy --prod --yes"
        path: directory relative to the project root (default .)
        timeout_s: timeout in seconds (default 600)
    """
    from .. import connect

    if service.strip().lower() == "github":
        return await gh.ainvoke({"command": command, "timeout_s": timeout_s})
    return await connect.run(service, command, cwd=_abs(path), timeout=timeout_s)


async def ensure_workspace() -> None:
    await sandbox.exec_("mkdir -p " + shlex.quote(_root()), cwd=config.workspace_root, timeout=30)


SANDBOX_TOOLS = [shell, write_file, read_file, list_files, git_push, gh, cli]
