"""Sign in once: connect a service's CLI with the human's browser session.

When the human signs in to a service in the agents' browser (Accounts page or Setup), Todd also signs in that
service's CLI right away, while they're there and the session is fresh (a fresh session is also what keeps sites from
asking for 2FA again). Runs then never need to authorize anything. Agents can start the same flow with cli_login.

Flow, all without anyone copying a token:
  1. Start the CLI's browser login in the sandbox, in a private HOME (the CLI keeps its sign-in there).
  2. Approve it in the shared browser (cdp.approve): fill the one-time code, click Approve, catch a localhost
     redirect and replay it where the CLI listens, or read the code the page shows and hand it to the CLI.
     Password and 2FA pages are left to the human, in the same tab.
  3. Store the result in the vault: the token itself when the CLI prints it (GitHub → GITHUB_TOKEN, which also powers
     the API toolset and git_push), otherwise the CLI's sign-in files as an encrypted snapshot (CLI_STATE_<SERVICE>)
     that `run` unpacks into a private dir only for the duration of each command, saving any refreshed tokens.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable

from . import cdp, vault
from .config import config
from .sdk import ToolError
from .tools import sandbox

log = logging.getLogger("todd.connect")

EXIT_MARK = "__TODD_EXIT__"
MAX_STATE_B64 = 96 * 1024  # a CLI's sign-in files are a few KB; the snapshot travels in one env var
CHUNK = 15000  # below the sandbox's output clip


@dataclass(frozen=True)
class Connector:
    service: str  # accounts/integrations id
    name: str
    run: str  # how to invoke the CLI
    login: str  # arguments that start its browser login
    url_re: str  # the approval URL in the login output
    hosts: tuple[str, ...]  # domains Todd may click on while approving
    code_re: str | None = None  # one-time code printed by the CLI (typed into the page if it asks)
    secret: str | None = None  # store the token under this vault name (with `token`)...
    token: str | None = None  # ...printed by these arguments; otherwise the sign-in files are snapshotted
    complete: str | None = None  # arguments that finish the login after approval ({code}: code from the page)
    page_code_re: str | None = None  # a code the page shows after approval, for `complete`
    callback: str | None = None  # the CLI waits for the browser to open this localhost URL
    blocked: tuple[str, ...] = ("login", "logout", "auth", "config", "token", "tokens")
    example: str = ""


CONNECTORS: dict[str, Connector] = {c.service: c for c in [
    Connector("github", "GitHub CLI", "gh", "auth login --web --hostname github.com --git-protocol https --scopes workflow",
              r"https://github\.com/login/device\S*", ("github.com",),
              code_re=r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})", secret="GITHUB_TOKEN",
              token="auth token --hostname github.com",
              blocked=("auth", "extension", "extensions", "ext", "alias", "config", "codespace", "cs"),
              example="repo create my-app --private --source . --push"),
    Connector("vercel", "Vercel CLI", "vercel", "login", r"https://vercel\.com/oauth/device\?user_code=[A-Z0-9-]+",
              ("vercel.com",), code_re=r"user_code=([A-Z0-9]{4}-[A-Z0-9]{4})",
              blocked=("login", "logout", "switch", "whoami --token"), example="deploy --prod --yes"),
    Connector("netlify", "Netlify CLI", "npx -y netlify-cli", "login", r"https://app\.netlify\.com/authorize\?\S+",
              ("netlify.com",), blocked=("login", "logout", "switch"), example="deploy --prod --dir dist"),
    Connector("railway", "Railway CLI", "npx -y @railway/cli", "login --browserless",
              r"https://railway\.com/activate\?user_code=[A-Z0-9-]+", ("railway.com",),
              code_re=r"user_code=([A-Z0-9]{4}-[A-Z0-9]{4})", blocked=("login", "logout"), example="up --detach"),
    Connector("cloudflare", "Cloudflare Wrangler", "npx -y wrangler", "login --browser=false",
              r"https://dash\.cloudflare\.com/oauth2/auth\?\S+", ("cloudflare.com",),
              callback="http://localhost:8976/", example="pages deploy dist --project-name my-site"),
    Connector("stripe", "Stripe CLI", "npx -y @stripe/cli", "login",
              r"https://access\.stripe\.com/stripecli/oauth2/device[^\s\"',]*", ("stripe.com",),
              code_re=r"\"verification_code\":\s*\"([A-Z0-9-]+)\"", complete="login --complete-device",
              example="products list --limit 5"),
    Connector("firebase", "Firebase CLI", "firebase", "login --no-localhost",
              r"https://auth\.firebase\.tools/login\?\S+", ("firebase.tools", "google.com"),
              complete="login {code}", page_code_re=r"\b(4/[0-9A-Za-z_\-]{20,})",
              blocked=("login", "login:ci", "login:add", "login:use", "logout"), example="deploy --only hosting"),
]}


def state_name(service: str) -> str:
    return f"CLI_STATE_{service.upper()}"


def connector(service: str) -> Connector:
    c = CONNECTORS.get(service.strip().lower())
    if c is None:
        raise ToolError(f"No browser sign-in for {service!r}. Supported: {', '.join(CONNECTORS)}. For other services "
                        "ask the human to add a key in Settings → Integrations.")
    return c


def is_connected(service: str) -> bool:
    c = CONNECTORS.get(service)
    if c is None:
        return False
    return vault.has_secret(c.secret) if c.secret else vault.has_secret(state_name(service))


def disconnect(service: str) -> None:
    c = connector(service)
    vault.delete_secret(c.secret or state_name(service))


# ------------------------------------------------------------------------------------------ sandbox helpers
def _root() -> str:
    return os.getenv("TODD_CLI_LOGIN_DIR", "/tmp/todd-login")


# Point the CLI at a private HOME in $STATE (where it keeps its sign-in); keep the npm cache so npx stays fast.
_ENV = ('REAL_HOME="$HOME"; export HOME="$STATE/home" XDG_CONFIG_HOME="$STATE/home/.config" '
        'XDG_DATA_HOME="$STATE/home/.local/share" XDG_STATE_HOME="$STATE/home/.local/state" '
        'XDG_CACHE_HOME="$STATE/cache" npm_config_cache="$REAL_HOME/.npm" GH_PROMPT_DISABLED=1 GH_BROWSER=true '
        'BROWSER=true\n')
# Each background CLI runs in its own process group ($! is its id), so npx → node → CLI all stop together.
_KILL = '[ -f "$STATE/pid" ] && for p in $(cat "$STATE/pid"); do kill -- -"$p" || kill "$p"; done 2>/dev/null\n'


async def _sh(script: str, timeout: int = 60, env: dict[str, str] | None = None) -> dict:
    return await sandbox.exec_(script, cwd=config.workspace_root, timeout=timeout, env=env)


def _exit_code(text: str) -> int | None:
    m = re.search(EXIT_MARK + r" (\d+)", text)
    return int(m.group(1)) if m else None


def _tail(text: str, n: int = 6) -> str:
    lines = [ln for ln in text.replace(EXIT_MARK, "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


async def _read(path: str) -> str:
    """Read a (base64) file from the sandbox in chunks below its output limit."""
    q = shlex.quote(path)
    size = int(((await _sh(f"wc -c < {q}"))["output"] or "0").strip() or 0)
    if size > MAX_STATE_B64:
        raise ToolError(f"The CLI's sign-in files are unexpectedly large ({size} bytes); not storing them.")
    parts = [(await _sh(f"tail -c +{off + 1} {q} | head -c {CHUNK}"))["output"] for off in range(0, size, CHUNK)]
    return "".join(parts)


_SNAPSHOT = ('(cd "$STATE/home" && tar czf - --exclude=./.npm --exclude=./.cache --exclude=./.local/state '
             '--exclude="*.log" . | base64 | tr -d "\\n" > "$STATE/state.b64")\n')


def _background(command: str, logname: str) -> str:
    """Run the CLI in the background (it waits for the approval), logging to $STATE/<logname>."""
    # (not `cd … && nohup … &`: that backgrounds a subshell which keeps this command's output open)
    return (_ENV + 'cd "$STATE" || exit 1\nset -m\n'
            + f'nohup bash -c {shlex.quote(command + f"; echo {EXIT_MARK} $?")} '
            f'> "$STATE/{logname}" 2>&1 < /dev/null &\necho $! >> "$STATE/pid"\nset +m\n')


# ------------------------------------------------------------------------------------------ connecting
@dataclass
class Connection:
    service: str
    state: str = "starting"  # starting | approving | needs_you | connected | failed
    message: str = ""
    code: str | None = None
    url: str | None = None
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def info(self) -> dict[str, Any]:
        return {"service": self.service, "name": CONNECTORS[self.service].name, "state": self.state,
                "message": self.message, "code": self.code, "url": self.url, "connected": self.state == "connected"}


_CONNECTIONS: dict[str, Connection] = {}


def status(service: str) -> dict[str, Any]:
    conn = _CONNECTIONS.get(service)
    if conn is not None and (conn.state != "connected" or is_connected(service)):
        return conn.info()
    c = connector(service)
    return {"service": service, "name": c.name, "state": "connected" if is_connected(service) else "idle",
            "message": "", "code": None, "url": None, "connected": is_connected(service)}


def start(service: str, on_change: Callable[[Connection], None] | None = None,
          browser_lock: asyncio.Lock | None = None) -> Connection:
    """Start connecting (or return the connection already in progress)."""
    c = connector(service)
    conn = _CONNECTIONS.get(c.service)
    if conn is not None and conn.task is not None and not conn.task.done():
        return conn
    conn = Connection(c.service)
    _CONNECTIONS[c.service] = conn
    conn.task = asyncio.create_task(_flow(c, conn, on_change, browser_lock), name=f"connect-{c.service}")
    return conn


def _set(conn: Connection, on_change: Any, state: str, message: str = "") -> None:
    conn.state, conn.message = state, message
    if on_change:
        try:
            on_change(conn)
        except Exception:  # noqa: BLE001
            log.exception("on_change failed")


async def _flow(c: Connector, conn: Connection, on_change: Any, browser_lock: asyncio.Lock | None) -> None:
    state = f"{_root()}/{c.service}"
    q = shlex.quote
    tab = watcher = None
    try:
        _set(conn, on_change, "starting", f"Starting the {c.name} sign-in")
        await _sh(f"STATE={q(state)}\n" + _KILL + 'rm -rf "$STATE" && mkdir -p "$STATE/home" && chmod 700 "$STATE"\n'
                  + _background(f"{c.run} {c.login}", "log"))
        out = ""
        for _ in range(240):  # npx may download the CLI first
            out = (await _sh(f"cat {q(state)}/log 2>/dev/null"))["output"] or ""
            url = re.search(c.url_re, out)
            if url and (not c.code_re or re.search(c.code_re, out)) or _exit_code(out) is not None:
                break
            await asyncio.sleep(1)
        url, code = re.search(c.url_re, out), re.search(c.code_re, out) if c.code_re else None
        if not url:
            raise ToolError(f"{c.name} didn't show a sign-in link:\n{_tail(out)}")
        conn.url, conn.code = url.group(0), code.group(1) if code else None
        cli_done = asyncio.Event()
        if not c.complete:  # the login command itself finishes once the page is approved
            watcher = asyncio.create_task(_watch(state, "log", cli_done))
        elif not c.page_code_re:  # a separate command finishes it (it waits for, or is retried until, the approval)
            watcher = asyncio.create_task(_complete(c, state, "", cli_done))
        else:  # it needs the code the page shows after approval
            watcher = None

        async def approve() -> dict:
            return await cdp.approve(conn.url, hosts=list(c.hosts), code=conn.code, callback=c.callback,
                                     page_code_re=c.page_code_re, done=cli_done.is_set,
                                     on_state=lambda s, m: _set(conn, on_change, s, m))

        if browser_lock is not None:
            async with browser_lock:
                res = await approve()
        else:
            res = await approve()
        tab = res.get("tab")
        if res["state"] != "approved":
            raise ToolError("The approval wasn't finished in time. Try again from the Accounts page.")
        if res.get("callback"):  # the browser can't reach the CLI's localhost; replay the redirect where it runs
            if not res["callback"].startswith(c.callback or "\0"):
                raise ToolError("Unexpected redirect; not forwarding it.")
            await _sh(f"curl -fsS --max-time 30 -o /dev/null {q(res['callback'])}")
        if res.get("page_code"):
            await asyncio.wait_for(_complete(c, state, res["page_code"], cli_done), 180)
        elif watcher is not None:
            rc = await asyncio.wait_for(watcher, 180)
            if rc != 0:
                raise ToolError(f"{c.name} sign-in failed (exit {rc}).")
        await _store(c, state)
        _set(conn, on_change, "connected", f"{c.name} is signed in with your account")
    except Exception as e:  # noqa: BLE001
        _set(conn, on_change, "failed", str(e) if isinstance(e, ToolError) else f"{type(e).__name__}: {e}")
    finally:
        if watcher is not None and not watcher.done():
            watcher.cancel()
        try:  # the CLI's sign-in leaves the sandbox before anyone is told it's done
            await _sh(f"STATE={q(state)}\n" + _KILL + 'rm -rf "$STATE"')
        except Exception:  # noqa: BLE001
            log.exception("cleanup of %s failed", state)
        if tab and conn.state == "connected":
            await cdp.close_tab(tab)
        conn.done.set()


async def _watch(state: str, logname: str, done: asyncio.Event) -> int:
    path = shlex.quote(f"{state}/{logname}")
    while True:
        rc = _exit_code((await _sh(f"cat {path} 2>/dev/null"))["output"] or "")
        if rc is not None:
            done.set()
            return rc
        await asyncio.sleep(2)


async def _complete(c: Connector, state: str, code: str, done: asyncio.Event) -> int:
    """Run the command that finishes the login, retrying while the page isn't approved yet."""
    args = c.complete.format(code=shlex.quote(code)) if c.complete else ""
    out = ""
    for _ in range(100):
        r = await _sh(f"STATE={shlex.quote(state)}\n" + _ENV + f'cd "$STATE" && {c.run} {args} < /dev/null 2>&1',
                      timeout=120)
        out = r.get("output") or ""
        if r.get("exit_code") == 0:
            done.set()
            return 0
        await asyncio.sleep(3)
    raise ToolError(f"{c.name} didn't finish signing in:\n{_tail(out)}")


async def _store(c: Connector, state: str) -> None:
    q = shlex.quote
    if c.token:
        r = await _sh(f"STATE={q(state)}\n" + _ENV + f"{c.run} {c.token}")
        token = (r.get("output") or "").strip()
        if r.get("exit_code") != 0 or not token or any(ch.isspace() for ch in token):
            raise ToolError(f"{c.name} signed in, but its token couldn't be read.")
        vault.set_secret(c.secret, token)  # type: ignore[arg-type]
        return
    await _sh(f"STATE={q(state)}\n" + _SNAPSHOT)
    blob = (await _read(f"{state}/state.b64")).strip()
    if not blob:
        raise ToolError(f"{c.name} signed in, but its sign-in files couldn't be saved.")
    vault.set_secret(state_name(c.service), blob)


# ------------------------------------------------------------------------------------------ using a connected CLI
_run_locks: dict[str, asyncio.Lock] = {}


async def run(service: str, command: str, cwd: str, timeout: int = 600) -> dict[str, Any]:
    """Run a connected CLI (not GitHub: that's the `gh` tool) with its saved sign-in. The snapshot is unpacked into a
    private dir for this command only; refreshed tokens are saved back; the dir is removed."""
    c = connector(service)
    blob = vault.get_secret(state_name(c.service))
    if not blob:
        raise ToolError(f"{c.name} isn't connected. Call cli_login(\"{c.service}\") first.")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ToolError(f"couldn't parse the command: {e}") from e
    joined = " ".join(argv)
    if not argv or any(joined == b or joined.startswith(b + " ") for b in c.blocked):
        raise ToolError(f"`{argv[0] if argv else ''}` isn't available through Todd (it signs in or out, or can print "
                        f"credentials). Blocked: {', '.join(c.blocked)}.")
    q = shlex.quote
    async with _run_locks.setdefault(c.service, asyncio.Lock()):  # one refresh at a time
        r = await sandbox.exec_(
            'STATE=$(mktemp -d /tmp/todd-cli-XXXXXX) && chmod 700 "$STATE" && mkdir -p "$STATE/home"\n'
            'printf %s "$TODD_CLI_STATE" | base64 -d | tar xzf - -C "$STATE/home"\n'
            'unset TODD_CLI_STATE\n' + _ENV
            + f"cd {q(cwd)} && {c.run} {shlex.join(argv)} < /dev/null 2>&1; rc=$?\n" + _SNAPSHOT
            + 'echo; echo "__TODD_RC__ $rc $STATE"',
            cwd=cwd, timeout=int(min(max(timeout, 10), 1800)) + 30, env={"TODD_CLI_STATE": blob})
        out = r.get("output") or ""
        m = re.search(r"__TODD_RC__ (\d+) (\S+)", out)
        text = out[:m.start()].rstrip() if m else out
        if m:
            tmp = m.group(2)
            try:  # keep tokens the CLI refreshed during the command
                fresh = (await _read(f"{tmp}/state.b64")).strip()
                if fresh and fresh != blob:
                    vault.set_secret(state_name(c.service), fresh)
            finally:
                await _sh(f"rm -rf {q(tmp)}")
        rc = int(m.group(1)) if m else r.get("exit_code")
        if r.get("timed_out"):
            text += "\n[timed out]"
        return {"exit_code": rc, "output": text}
