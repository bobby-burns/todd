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
# Local git config that could redirect a push, capture credentials or run code while a command holds the token.
_GIT_GUARD = ("bad=$(git config --local --get-regexp "
              "'^(url\\..*|credential\\..*|core\\.sshcommand|core\\.hookspath|core\\.askpass|core\\.fsmonitor|"
              "core\\.gitproxy|core\\.pager|core\\.editor|sequence\\.editor|http\\..*|include\\..*|includeif\\..*|"
              "alias\\..*|filter\\..*|diff\\..*\\.textconv|diff\\..*\\.command|merge\\..*\\.driver|protocol\\..*|"
              "uploadpack\\..*|remote\\..*\\.(uploadpack|receivepack|proxy)|gpg\\.program|gpg\\..*\\.program)$' "
              "2>/dev/null); "
              "if [ -n \"$bad\" ]; then echo \"refusing to run with a token: suspicious git config: $bad\"; exit 3; fi")
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
async def shell(cmd: str, timeout_s: int = 300, env: dict[str, str] | None = None,
                save_to_vault: dict[str, str] | None = None) -> dict:
    """Run a bash command in the project directory. Returns exit_code and combined stdout/stderr. Prefer CLIs and
    APIs (npx vercel, npx stripe, firebase, npx wrangler, curl) over the browser; pass credentials through `env`
    as {{secret:NAME}} so they're never in the command text. When a command prints a new secret (a webhook signing
    secret, a generated key), pass save_to_vault so it goes straight into the vault instead of into your context.

    Args:
        cmd: bash command (non-interactive)
        timeout_s: timeout in seconds (default 300, max 1800)
        env: extra environment variables; values may be {{secret:NAME}} (e.g. {"STRIPE_API_KEY": "{{secret:STRIPE_SECRET_KEY}}"})
        save_to_vault: {"NAME": "regex"}: the first match (its first group, if it has one) is stored as NAME and shown
            to you as {{secret:NAME}}, e.g. {"STRIPE_WEBHOOK_SECRET": "(whsec_[A-Za-z0-9]+)"}
    """
    from .infra import resolve_secrets

    resolved = {k: resolve_secrets(str(v)) for k, v in (env or {}).items()}
    r = await sandbox.exec_(cmd, cwd=_root(), timeout=int(min(max(timeout_s, 5), 1800)), env=resolved)
    if save_to_vault:
        r = dict(r)
        r["output"], r["saved_to_vault"] = _capture(str(r.get("output") or ""), save_to_vault)
    return r


def _capture(output: str, fields: dict[str, str]) -> tuple[str, list[str]]:
    """Store what each regex finds in `output` in the vault; the output shows {{secret:NAME}} in its place."""
    from .. import vault
    from .page_capture import check_name

    saved = []
    for name, pattern in fields.items():
        check_name(name)
        try:
            m = re.search(pattern, output)
        except re.error as e:
            raise ToolError(f"bad regex for {name}: {e}") from e
        if not m:
            continue
        value = (m.group(1) if m.groups() else m.group(0)).strip()
        if not value:
            continue
        vault.set_secret(name, value)
        output = output.replace(value, f"{{{{secret:{name}}}}}")
        saved.append(name)
    return output, saved


@todd_tool(toolset="sandbox")
async def write_file(path: str, content: str) -> str:
    """Create or overwrite a file.

    Args:
        path: path relative to the project root
        content: full file content
    """
    from .. import redact

    if redact.hidden_markers(content):
        raise ToolError("This content has values Todd hid from you (••••••••, [secret hidden]); writing it would replace "
                        "the real ones. Change only the lines you need with shell (e.g. `sed -i` or `printf 'NAME=%s\\n' "
                        "\"$VALUE\" >> .env` with env={\"VALUE\": \"{{secret:NAME}}\"}).")
    await sandbox.write(_abs(path), content)
    return f"wrote {path} ({len(content)} chars)"


@todd_tool(toolset="sandbox")
async def read_file(path: str) -> str:
    """Read a file.

    Args:
        path: path relative to the project root
    """
    content = (await sandbox.read(_abs(path)))["content"]
    from ..workspace import ENV_FILE, ENV_LINE, MASK

    if ENV_FILE.search(path):  # settings files: names, not values (they're usually keys)
        content = ENV_LINE.sub(lambda m: m.group(1) + MASK, content) + \
            "\n[values hidden: set them with shell env={{secret:NAME}}, never by reading them]"
    return content


@todd_tool(toolset="sandbox")
async def list_files(path: str = ".", depth: int = 2) -> dict:
    """List files and directories (skips node_modules, .git, .next).

    Args:
        path: directory relative to the project root (default .)
        depth: how many levels deep (default 2)
    """
    return await sandbox.listdir(_abs(path), depth=int(depth))


# Git with GitHub sign-in: the token reaches git only through gh's credential helper, for github.com, for one
# command; hooks are off and the repo's config is checked first. The sandbox itself has no GitHub credentials.
_GIT_AUTH_ENV = ("export GIT_TERMINAL_PROMPT=0 GH_HOST=github.com GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1 "
                 "GIT_ALLOW_PROTOCOL=https:http:ssh:git:file "
                 "GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null "
                 "GIT_CONFIG_KEY_1=credential.https://github.com.helper GIT_CONFIG_VALUE_1= "
                 "GIT_CONFIG_KEY_2=credential.https://github.com.helper GIT_CONFIG_VALUE_2='!gh auth git-credential'")
_GIT_NETWORK = {"push", "pull", "fetch", "clone", "ls-remote", "submodule"}
_GIT_ALLOWED = _GIT_NETWORK | {
    "add", "bisect", "blame", "branch", "checkout", "cherry-pick", "clean", "commit", "describe", "diff", "grep",
    "init", "log", "ls-files", "merge", "mv", "rebase", "remote", "reset", "restore", "revert", "rev-parse", "rm",
    "shortlog", "show", "stash", "status", "switch", "tag"}
# Options that would run a program, load other config, or need an editor.
_GIT_BAD_OPTS = ("--upload-pack", "--receive-pack", "--exec", "--template", "--config", "--interactive",
                 "--edit-description", "--ext-diff", "--textconv")
DEFAULT_GITIGNORE = "node_modules/\n.env\n.env.*\n!.env.example\n.next/\n.expo/\n.vercel/\n.turbo/\n*.p8\n*.pem\n.DS_Store\n"


def _git_argv(command: str) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ToolError(f"couldn't parse the arguments: {e}") from e
    if argv and argv[0] == "git":
        argv = argv[1:]
    if not argv or argv[0].startswith("-"):
        raise ToolError("Start with the git subcommand, e.g. \"push -u origin main\" (no global options like -c or "
                        "-C: use `path` for the directory).")
    sub = argv[0]
    if sub not in _GIT_ALLOWED:
        raise ToolError(f"`git {sub}` isn't available here. Allowed: {', '.join(sorted(_GIT_ALLOWED))}. "
                        "(git config can't be changed through Todd; user.name/email are set for you.)")
    for a in argv[1:]:
        if a.split("=", 1)[0] in _GIT_BAD_OPTS or (a in ("-c", "-i") and sub in ("clone", "rebase", "add", "submodule")):
            raise ToolError(f"`{a}` isn't allowed with git {sub} here (it would run a program, load config or open "
                            "an editor).")
        if a == "-u" and sub in ("fetch", "pull", "clone", "ls-remote"):  # -u means --upload-pack for these
            raise ToolError(f"`-u` isn't allowed with git {sub} (it runs a program).")
    return argv


async def _git_run(argv: list[str], path: str, timeout: int, pre: str = "", tail: str | None = None) -> dict:
    """Run git in the project (or `path`), with GitHub sign-in for commands that talk to a remote. With the token,
    hooks are off and the repo's config is checked before anything else runs. `pre` runs first (same safety);
    `tail` replaces the git command line (for git_push's shell-expanded refspec)."""
    network = argv[0] in _GIT_NETWORK
    token = vault.get_secret("GITHUB_TOKEN") if network else None
    if network and not token:
        raise ToolError(NO_TOKEN)
    lines = ["set -o pipefail"]
    if network:
        lines += [_GIT_AUTH_ENV, f"(! git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {{ {_GIT_GUARD}; }})"]
    lines += ["(git config user.name >/dev/null 2>&1 || git config --global user.name 'Todd Agent')",
              "(git config user.email >/dev/null 2>&1 || git config --global user.email 'todd@localhost')"]
    if pre:
        lines.append(pre)
    lines.append((tail or shlex.join(["git", *argv])) + " 2>&1")
    r = await sandbox.exec_(" && ".join(lines), cwd=_abs(path), timeout=timeout,
                            env={"GH_TOKEN": token} if token else None)
    out = r.get("output") or ""
    if token:
        for v in (token, base64.b64encode(f"x-access-token:{token}".encode()).decode()):
            out = out.replace(v, "***")
    return {"exit_code": r.get("exit_code"), "output": out}


@todd_tool(toolset="sandbox")
async def git(command: str, path: str = ".", timeout_s: int = 300) -> dict:
    """Run git the normal way, signed in to GitHub (the human's account) for push/pull/fetch/clone. Use this instead
    of `shell` for anything that talks to GitHub: the sandbox has no GitHub credentials of its own. Examples:
    "push -u origin main", "pull --rebase", "clone https://github.com/owner/repo", "remote add origin
    https://github.com/owner/repo.git", "status", "log --oneline -5", "checkout -b feature/x".
    For push/pull/fetch/clone hooks are off; git config can't be changed here (never put a token in a remote URL).

    Args:
        command: everything after `git`, e.g. "push -u origin main"
        path: directory relative to the project root (default .)
        timeout_s: timeout in seconds (default 300)
    """
    return await _git_run(_git_argv(command), path, int(min(max(timeout_s, 5), 1800)))


@todd_tool(toolset="sandbox")
async def git_push(repo: str | None = None, message: str = "Update from Todd", branch: str | None = None,
                   force: bool = False, path: str = ".") -> dict:
    """Commit everything and push it to GitHub in one step (a normal `git push -u origin`). Creates the repo's git
    history if there is none, adds a sensible .gitignore if the project has none (node_modules, .env, build
    folders, key files), points `origin` at `repo` when you give one, and pushes the current branch (or `branch`)
    with upstream tracking, so later `git("pull")` / `git("push")` just work. For anything else use the `git` tool.

    Args:
        repo: GitHub repo as owner/name (sets origin); omit to push to the existing origin
        message: commit message (only used when there are changes to commit)
        branch: remote branch to push to (default: the current branch; "main" for a new repository)
        force: overwrite the remote branch (--force-with-lease: refuses if someone else pushed meanwhile)
        path: directory relative to the project root (default .)
    """
    if repo is not None and (not _REPO.fullmatch(repo) or ".." in repo or repo.endswith(".git")):
        raise ToolError("repo must look like owner/name")
    if branch is not None and (not _BRANCH.fullmatch(branch) or ".." in branch or branch.startswith("-")):
        raise ToolError("invalid branch name")
    q = shlex.quote
    url = f"https://github.com/{repo}.git" if repo else ""
    pre = " && ".join([
        f"(git rev-parse --is-inside-work-tree >/dev/null 2>&1 || git init -q -b {q(branch or 'main')})",
        _GIT_GUARD,  # a repo that existed already was checked before this; a new one is checked here
        f"([ -e .gitignore ] || printf %s {q(DEFAULT_GITIGNORE)} > .gitignore)",
        "git add -A",
        f"(git diff --cached --quiet || git commit -q --no-verify -m {q(message)})",
        (f"(git remote get-url origin >/dev/null 2>&1 && git remote set-url origin {q(url)} "
         f"|| git remote add origin {q(url)})") if repo else
        "(git remote get-url origin >/dev/null 2>&1 || { echo 'No origin remote yet: pass repo=\"owner/name\".'; exit 4; })",
        'BR=$(git symbolic-ref --short HEAD)',
    ])
    dest = q(branch) if branch else '"$BR"'
    tail = (f"git push -u {'--force-with-lease ' if force else ''}origin \"HEAD:refs/heads/\"{dest} "
            "&& git log --oneline -1")
    return await _git_run(["push"], path, 300, pre=pre, tail=tail)


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
        vault.set_secret("ASC_PRIVATE_KEY", pem.strip(), track=False)
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


SANDBOX_TOOLS = [shell, write_file, read_file, list_files, git, git_push, gh, cli, eas]
