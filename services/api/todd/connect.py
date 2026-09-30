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
import fnmatch
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable

from . import cdp, settings, vault
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
    callback: str | None = None  # the CLI waits for the browser to open this localhost URL (`*` = any port/text)
    blocked: tuple[str, ...] = ("login", "logout", "auth", "config", "token", "tokens")
    example: str = ""


# The image has eas-cli installed; older sandbox images fall back to npx.
EAS_RUN = '$(command -v eas >/dev/null 2>&1 && echo eas || echo "npx -y eas-cli@latest")'


def _npm_cli(package: str, binary: str) -> str:
    """A CLI from npm, run from Todd's own install (todd-cli), never the project's node_modules; older images: npx."""
    return f'$(command -v todd-cli >/dev/null 2>&1 && echo "todd-cli {package} {binary}" || echo "npx -y {package}")'


CONNECTORS: dict[str, Connector] = {c.service: c for c in [
    Connector("github", "GitHub CLI", "gh", "auth login --web --hostname github.com --git-protocol https --scopes workflow",
              r"https://github\.com/login/device\S*", ("github.com",),
              code_re=r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})", secret="GITHUB_TOKEN",
              token="auth token --hostname github.com",
              blocked=("auth", "extension", "extensions", "ext", "alias", "config", "codespace", "cs"),
              example="repo create my-app --private --source . --push"),
    Connector("vercel", "Vercel CLI", "vercel", "login", r"https://vercel\.com/oauth/device\?user_code=[A-Z0-9-]+",
              ("vercel.com",), code_re=r"user_code=([A-Z0-9]{4}-[A-Z0-9]{4})",
              blocked=("login", "logout", "switch", "whoami --token"), example="deploy --target=preview --yes"),
    Connector("netlify", "Netlify CLI", _npm_cli("netlify-cli", "netlify"), "login",
              r"https://app\.netlify\.com/authorize\?\S+", ("netlify.com",),
              blocked=("login", "logout", "switch", "env:get", "env:list"), example="deploy --dir dist"),
    Connector("railway", "Railway CLI", _npm_cli("@railway/cli", "railway"), "login --browserless",
              r"https://railway\.com/activate\?user_code=[A-Z0-9-]+", ("railway.com",),
              code_re=r"user_code=([A-Z0-9]{4}-[A-Z0-9]{4})", blocked=("login", "logout"), example="up --detach"),
    Connector("cloudflare", "Cloudflare Wrangler", _npm_cli("wrangler", "wrangler"), "login --browser=false",
              r"https://dash\.cloudflare\.com/oauth2/auth\?\S+", ("cloudflare.com",),
              callback="http://localhost:8976/", example="pages deploy dist --project-name my-site"),
    Connector("stripe", "Stripe CLI", _npm_cli("@stripe/cli", "stripe"), "login",
              r"https://access\.stripe\.com/stripecli/oauth2/device[^\s\"',]*", ("stripe.com",),
              code_re=r"\"verification_code\":\s*\"([A-Z0-9-]+)\"", complete="login --complete-device",
              example="products list --limit 5"),
    # eas-cli's browser login waits on a random localhost port; Todd catches the redirect and replays it there.
    Connector("expo", "Expo EAS CLI", EAS_RUN, "login --browser", r"https://expo\.dev/login\?\S+", ("expo.dev",),
              callback="http://localhost:*/auth/callback",
              blocked=("login", "logout", "account:login", "account:logout"),
              example="build:list --limit 1 --non-interactive"),
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
    """Everything here handles a sign-in, so it runs in the signed-in runner (when there is one)."""
    return await sandbox.exec_(script, cwd=config.workspace_root, timeout=timeout, env=env, signed_in=True)


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


def start(service: str, on_change: Callable[[Connection], None] | None = None) -> Connection:
    """Start connecting (or return the connection already in progress). The approval waits its turn for the browser
    (the one lock every run and sign-in shares)."""
    c = connector(service)
    conn = _CONNECTIONS.get(c.service)
    if conn is not None and conn.task is not None and not conn.task.done():
        return conn
    conn = Connection(c.service)
    _CONNECTIONS[c.service] = conn
    conn.task = asyncio.create_task(_flow(c, conn, on_change), name=f"connect-{c.service}")
    return conn


def auto_connect(accounts: list[dict[str, Any]]) -> str | None:
    """Sign in once, by default: when an account with a CLI is signed in to the browser but its CLI isn't connected,
    connect it. One at a time (they share the browser), each tried once per Todd start (the Connect button retries),
    and never a CLI the human disconnected. Returns the service it started, if any."""
    if any(cn.task is not None and not cn.task.done() for cn in _CONNECTIONS.values()):
        return None
    skip = set(settings.get("cli_auto_skip") or [])
    for a in accounts:
        sid = a.get("id")
        if sid in CONNECTORS and a.get("status") == "signed_in" and sid not in _CONNECTIONS and sid not in skip \
                and not is_connected(sid):
            start(sid)
            return sid
    return None


def set_auto(service: str, on: bool) -> None:
    skip = [s for s in (settings.get("cli_auto_skip") or []) if s != service]
    settings.update({"cli_auto_skip": skip if on else [*skip, service]})


def _set(conn: Connection, on_change: Any, state: str, message: str = "") -> None:
    conn.state, conn.message = state, message
    if on_change:
        try:
            on_change(conn)
        except Exception:  # noqa: BLE001
            log.exception("on_change failed")


async def _flow(c: Connector, conn: Connection, on_change: Any) -> None:
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

        from .runtime import browser_lock

        lock = browser_lock()
        if lock.locked():
            _set(conn, on_change, "starting", "Waiting for the browser: an agent is using it")
        await lock.acquire(f"sign-in:{c.service}")
        try:
            res = await approve()
        finally:
            lock.release(f"sign-in:{c.service}")
        tab = res.get("tab")
        if res["state"] != "approved":
            raise ToolError("The approval wasn't finished in time. Try again from the Accounts page.")
        if res.get("callback"):  # the browser can't reach the CLI's localhost; replay the redirect where it runs
            if not callback_ok(c, conn.url or "", res["callback"]):
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


def callback_ok(c: Connector, login_url: str, caught: str) -> bool:
    """Only replay the CLI's own sign-in redirect in the sandbox: plain http to localhost on a port, no user info or
    fragment, matching the connector's pattern, and (when the login link says where it wants the redirect and with
    which state) exactly that port, path and state."""
    from urllib.parse import parse_qs, urlsplit

    if not c.callback:
        return False
    try:
        u = urlsplit(caught)
        port = u.port
    except ValueError:
        return False
    if u.scheme != "http" or u.hostname != "localhost" or u.username or u.password or u.fragment or not port:
        return False
    if not fnmatch.fnmatchcase(f"http://localhost:{port}{u.path}", c.callback + ("" if "*" in c.callback else "*")):
        return False
    want = parse_qs(urlsplit(login_url).query)
    if want.get("redirect_uri"):
        r = urlsplit(want["redirect_uri"][0])
        if (r.hostname, r.port, r.path) != ("localhost", port, u.path):
            return False
    if want.get("state") and parse_qs(u.query).get("state") != want["state"]:
        return False
    return True


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
        vault.set_secret(c.secret, token, source="sign-in")  # type: ignore[arg-type]
        return
    await _sh(f"STATE={q(state)}\n" + _SNAPSHOT)
    blob = (await _read(f"{state}/state.b64")).strip()
    if not blob:
        raise ToolError(f"{c.name} signed in, but its sign-in files couldn't be saved.")
    vault.set_secret(state_name(c.service), blob, source="sign-in")


# ------------------------------------------------------------------------------------------ using a connected CLI
_run_locks: dict[str, asyncio.Lock] = {}


def _blocked(argv: list[str], rule: str) -> bool:
    """A blocked subcommand anywhere among the command's words, not just first: global flags in front
    (`stripe --color off config --list`) don't get around it. Rules with a flag need every part present."""
    parts = rule.split()
    if any(p.startswith("-") for p in parts):
        given = set(argv) | {a.split("=", 1)[0] for a in argv if a.startswith("-")}  # --token=x counts as --token
        return all(p in given for p in parts)
    words = [a for a in argv if not a.startswith("-")]
    return any(words[i:i + len(parts)] == parts for i in range(len(words)))


async def run(service: str, command: str, cwd: str, timeout: int = 600, env: dict[str, str] | None = None,
              pre: str = "", post: str = "") -> dict[str, Any]:
    """Run a connected CLI (not GitHub: that's the `gh` tool) with its saved sign-in. The snapshot is unpacked into a
    private dir for this command only; refreshed tokens are saved back; the dir is removed. `env`, `pre` and `post`
    let a tool add settings and shell lines around the command (e.g. the `eas` tool's App Store Connect key)."""
    c = connector(service)
    blob = vault.get_secret(state_name(c.service))
    if not blob:
        raise ToolError(f"{c.name} isn't connected. Call cli_login(\"{c.service}\") first.")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ToolError(f"couldn't parse the command: {e}") from e
    if not argv or any(_blocked(argv, b) for b in c.blocked):
        raise ToolError(f"`{argv[0] if argv else ''}` isn't available through Todd (it signs in or out, or can print "
                        f"credentials). Blocked: {', '.join(c.blocked)}.")
    q = shlex.quote
    async with _run_locks.setdefault(c.service, asyncio.Lock()):  # one refresh at a time
        r = await sandbox.exec_(
            'STATE=$(mktemp -d /tmp/todd-cli-XXXXXX) && chmod 700 "$STATE" && mkdir -p "$STATE/home"\n'
            'printf %s "$TODD_CLI_STATE" | base64 -d | tar xzf - -C "$STATE/home"\n'
            'unset TODD_CLI_STATE\n' + _ENV + (pre + "\n" if pre else "")
            + f"cd {q(cwd)} && {c.run} {shlex.join(argv)} < /dev/null 2>&1; rc=$?\n" + (post + "\n" if post else "")
            + _SNAPSHOT + 'echo; echo "__TODD_RC__ $rc $STATE"',
            cwd=cwd, timeout=int(min(max(timeout, 10), 1800)) + 30, env={**(env or {}), "TODD_CLI_STATE": blob},
            signed_in=True)
        out = r.get("output") or ""
        m = re.search(r"__TODD_RC__ (\d+) (\S+)", out)
        text = out[:m.start()].rstrip() if m else out
        if m:
            tmp = m.group(2)
            try:  # keep tokens the CLI refreshed during the command
                fresh = (await _read(f"{tmp}/state.b64")).strip()
                if fresh and fresh != blob:
                    vault.set_secret(state_name(c.service), fresh, track=False)
            finally:
                await _sh(f"rm -rf {q(tmp)}")
        rc = int(m.group(1)) if m else r.get("exit_code")
        if r.get("timed_out"):
            text += "\n[timed out]"
        return {"exit_code": rc, "output": text}
