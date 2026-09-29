"""The `sandbox` toolset: a Linux box (node 22, pnpm, git, gh, python, vercel + firebase + eas CLIs) with a workspace
shared by every agent in the run, plus an authenticated git_push, GitHub CLI (`gh`) and Expo EAS CLI (`eas`)."""

from __future__ import annotations

import asyncio
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
    For GitHub use `gh`; for Expo / EAS (mobile apps) use `eas`.

    Args:
        service: vercel, netlify, railway, cloudflare (wrangler), stripe or firebase
        command: arguments for the CLI, e.g. "deploy --prod --yes"
        path: directory relative to the project root (default .)
        timeout_s: timeout in seconds (default 600)
    """
    from .. import connect

    svc = service.strip().lower()
    if svc == "github":
        return await gh.ainvoke({"command": command, "timeout_s": timeout_s})
    if svc in ("expo", "eas"):
        return await eas.ainvoke({"command": command, "path": path, "timeout_s": timeout_s})
    return await connect.run(service, command, cwd=_abs(path), timeout=timeout_s)


# ------------------------------------------------------------------------------------------ Expo / EAS (mobile apps)
NO_EXPO = ("Expo isn't connected. Call cli_login(\"expo\"): Todd signs the EAS CLI in with the browser's Expo "
           "session (request_signins([\"expo\"]) first if the browser isn't signed in). An EXPO_TOKEN in the vault "
           "works too.")
# Where the App Store Connect key is written for one eas command: the sandbox user's real home (not the project, so it
# can't be committed; not the CLI's private sign-in dir, so it isn't saved with it).
ASC_KEY_FILE = ".todd-asc/AuthKey.p8"
ASC_KEY_PATH = "/home/agent/" + ASC_KEY_FILE  # what eas.json's submit `ascApiKeyPath` should say
# Subcommands that sign in or out (Todd handles that) or only work with a person at a terminal.
_EAS_BLOCKED = {"login", "logout", "account:login", "account:logout"}
_EAS_INTERACTIVE = {"credentials", "credentials:configure-build", "build:configure", "device:create"}
_eas_lock = asyncio.Lock()
_PEM = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)
_KEY_PRE = "\n".join([
    'KEYHOME="${REAL_HOME:-$HOME}"',
    # never leave the key behind: not from an earlier command that was killed, not after this one however it ends
    f'rm -f "$KEYHOME/{ASC_KEY_FILE}"',
    f"trap 'rm -f \"$KEYHOME/{ASC_KEY_FILE}\"' EXIT INT TERM HUP",
    'if [ -n "$TODD_ASC_KEY" ]; then',
    '  mkdir -p "$KEYHOME/.todd-asc" && chmod 700 "$KEYHOME/.todd-asc"',
    f'  export EXPO_ASC_API_KEY_PATH="$KEYHOME/{ASC_KEY_FILE}"',
    '  (umask 077 && printf %s "$TODD_ASC_KEY" > "$EXPO_ASC_API_KEY_PATH")',
    "fi",
    "unset TODD_ASC_KEY",
    # plain settings go in the script: env values are redacted from the output
    "export EXPO_NO_TELEMETRY=1 EAS_BUILD_NO_EXPO_GO_WARNING=true CI=1",
])
_KEY_POST = f'rm -f "$KEYHOME/{ASC_KEY_FILE}"'


def normalize_pem(raw: str) -> str:
    """The .p8 key as a well-formed PEM, however it was pasted: whole file, newlines lost by a one-line input
    (spaces instead), just the base64 body, or the whole file base64-encoded."""
    s = (raw or "").strip()
    if s.startswith("LS0tLS1CRUdJTi"):  # base64 of "-----BEGIN"
        try:
            s = base64.b64decode(s).decode().strip()
        except Exception:  # noqa: BLE001
            pass
    m = _PEM.search(s)
    label, body = (m.group(1), m.group(2)) if m else ("PRIVATE KEY", s)
    body = re.sub(r"\s+", "", body)
    if not body or not re.fullmatch(r"[A-Za-z0-9+/=]+", body):
        raise ToolError("ASC_PRIVATE_KEY doesn't look like an App Store Connect .p8 key. Set it up again with "
                        "find_integrations([\"app store connect\"]) or ask the human to re-add it in Settings → Vault.")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def _apple_env() -> tuple[dict[str, str], str]:
    """EAS's App Store Connect API key settings (when the key is in the vault) and a note for the agent."""
    key_id, issuer, key = (vault.get_secret(n) for n in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"))
    if not (key_id and issuer and key):
        return {}, ("No App Store Connect API key in the vault (ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY): set it "
                    "up in the browser first, see find_integrations([\"app store connect\"]).")
    pem = normalize_pem(key)
    if pem.strip() != key.strip():  # store it as EAS sees it, so the vault's output scrubbing matches it exactly
        vault.set_secret("ASC_PRIVATE_KEY", pem.strip())
    env = {"TODD_ASC_KEY": pem, "EXPO_ASC_KEY_ID": key_id.strip(), "EXPO_ASC_ISSUER_ID": issuer.strip()}
    team = vault.get_secret("APPLE_TEAM_ID")
    if team:
        env["EXPO_APPLE_TEAM_ID"] = team.strip()
        env["EXPO_APPLE_TEAM_TYPE"] = (vault.get_secret("APPLE_TEAM_TYPE") or "INDIVIDUAL").strip().upper()
    return env, f"App Store Connect API key passed to EAS (EXPO_ASC_*; key file at {ASC_KEY_PATH} during the command)."


@todd_tool(toolset="sandbox")
async def eas(command: str, path: str = ".", timeout_s: int = 900) -> dict:
    """Run the Expo EAS CLI (iPhone/Android apps built with Expo / React Native) in a project directory, signed in
    with the human's Expo account (connected once with cli_login("expo") from the browser session, or an EXPO_TOKEN
    in the vault). When the vault has the App Store Connect API key (ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY,
    optional APPLE_TEAM_ID), EAS gets it too, so it can manage iOS signing and upload to TestFlight; during the command
    the key file is at /home/agent/.todd-asc/AuthKey.p8 (use that for eas.json's submit `ascApiKeyPath`).
    There's no TTY: always pass --non-interactive where the command takes it.
    Examples: eas("init --non-interactive --force"), eas("build -p ios --profile production --non-interactive
    --no-wait"), eas("build:list -p ios --limit 1 --json --non-interactive"), eas("build:view <id> --json"),
    eas("submit -p ios --latest --profile production --non-interactive"), eas("whoami").

    Args:
        command: everything after `eas`, e.g. "build -p ios --profile production --non-interactive --no-wait"
        path: the Expo project directory relative to the project root (default .)
        timeout_s: timeout in seconds (default 900, max 1800). Start builds with --no-wait and poll build:view.
    """
    from .. import connect

    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ToolError(f"couldn't parse the arguments: {e}") from e
    sub = argv[0] if argv else ""
    if not argv or sub in _EAS_BLOCKED:
        raise ToolError(f"`eas {sub}` isn't available here: Todd signs EAS in (cli_login(\"expo\")).")
    if sub in _EAS_INTERACTIVE:
        raise ToolError(f"`eas {sub}` only works with a person at a terminal. Ask the human (ask_human) to run "
                        f"`npx eas-cli {shlex.join(argv)}` in the project on their computer, then continue.")
    token = vault.get_secret("EXPO_TOKEN")
    if not token and not connect.is_connected("expo"):
        raise ToolError(NO_EXPO)
    apple, note = _apple_env()
    timeout = int(min(max(timeout_s, 10), 1800))
    async with _eas_lock:  # the key file is shared by every eas command, so one at a time
        if token:
            script = "\n".join([
                _KEY_PRE,
                'if command -v eas >/dev/null 2>&1; then EAS=eas; else EAS="npx -y eas-cli@latest"; fi',
                f"$EAS {shlex.join(argv)} < /dev/null 2>&1; rc=$?",
                _KEY_POST,
                "exit $rc",
            ])
            r = await sandbox.exec_(script, cwd=_abs(path), timeout=timeout, env={"EXPO_TOKEN": token, **apple})
        else:
            r = await connect.run("expo", shlex.join(argv), cwd=_abs(path), timeout=timeout, env=apple,
                                  pre=_KEY_PRE, post=_KEY_POST)
    out = r.get("output") or ""
    for v in (token or "", apple.get("TODD_ASC_KEY", "")):
        if v:
            out = out.replace(v, "***")
    if r.get("timed_out"):
        out += "\n[timed out: start long builds with --no-wait and poll `build:view <id> --json`]"
    return {"exit_code": r.get("exit_code"), "output": out, "apple": note}


async def ensure_workspace() -> None:
    await sandbox.exec_("mkdir -p " + shlex.quote(_root()), cwd=config.workspace_root, timeout=30)


SANDBOX_TOOLS = [shell, write_file, read_file, list_files, git_push, gh, cli, eas]
