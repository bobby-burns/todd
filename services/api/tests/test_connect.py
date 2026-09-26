"""Sign in once: connecting CLIs with the human's browser session (connect.py, cli_login, cli, gh), routing to
"connect"/"cli" instead of the browser, the approval driver (cdp.approve) against fake provider pages, and copying
text/keys out of the browser without credentials passing through the model."""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from todd import cdp, connect, integrations, vault
from todd.config import config
from todd.db import Interaction, select, session
from todd.runtime import RunContext, set_current_agent
from todd.sdk import ToolError, set_ctx
from todd.tools import cli_login as cl
from todd.tools import infra, sandbox, sandbox_tools

from .helpers import new_run

# Stand-ins for real CLIs: a browser login that completes once "$HOME/../approved" exists (what approving the page
# does at the provider), keeping its sign-in in $HOME like the real ones.
FAKE_GH = r"""#!/bin/bash
case "$1 $2" in
  "auth login")
    echo "! First copy your one-time code: ABCD-1234"
    echo "Open this URL to continue in your web browser: https://github.com/login/device"
    while [ ! -f "$HOME/../approved" ]; do sleep 0.1; done
    mkdir -p "$XDG_CONFIG_HOME/gh" && echo "gho_fromthebrowser1234567890" > "$XDG_CONFIG_HOME/gh/hosts"
    echo "Logged in as tester";;
  "auth token") cat "$XDG_CONFIG_HOME/gh/hosts";;
  "api user") echo "host=$GH_HOST token=$GH_TOKEN";;
  *) echo "gh $*";;
esac
"""
FAKE_VERCEL = r"""#!/bin/bash
auth="$XDG_DATA_HOME/com.vercel.cli/auth.json"
case "$1" in
  login)
    echo "> NOTE: telemetry https://vercel.com/docs/cli/about-telemetry"
    echo "  Visit https://vercel.com/oauth/device?user_code=WXYZ-2345"
    while [ ! -f "$HOME/../approved" ]; do sleep 0.1; done
    mkdir -p "$(dirname "$auth")" && echo '{"session":"one"}' > "$auth"; echo "Success!";;
  deploy)
    echo "deploying $* as $(cat "$auth" 2>/dev/null || echo nobody)"
    echo '{"session":"two"}' > "$auth";;  # the CLI refreshed its session
  *) echo "vercel $*";;
esac
"""


@pytest.fixture
def local_sandbox(tmp_path, monkeypatch):
    """Run sandbox commands locally, with the fake CLIs first on PATH."""
    bindir, work = tmp_path / "bin", tmp_path / "work"
    bindir.mkdir()
    work.mkdir()
    for name, body in (("gh", FAKE_GH), ("vercel", FAKE_VERCEL)):
        f = bindir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    calls: list[dict] = []

    async def exec_(cmd, cwd, timeout=300, env=None):
        calls.append({"cmd": cmd, "env": dict(env or {})})
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", cmd, cwd=str(work), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", **(env or {})})
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 5)
        return {"exit_code": proc.returncode, "output": out.decode(), "timed_out": False}

    logins = tmp_path / "logins"
    monkeypatch.setattr(sandbox, "exec_", exec_)
    monkeypatch.setattr(config, "workspace_root", str(work))
    monkeypatch.setenv("TODD_CLI_LOGIN_DIR", str(logins))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for name in ("GITHUB_TOKEN", connect.state_name("vercel")):
        vault.delete_secret(name)
    connect._CONNECTIONS.clear()
    yield SimpleNamespace(calls=calls, logins=logins, work=work)
    for name in ("GITHUB_TOKEN", connect.state_name("vercel")):
        vault.delete_secret(name)


@pytest.fixture
def fake_approval(monkeypatch, local_sandbox):
    """cdp.approve without a browser: approving = the provider marking the login approved."""
    seen: dict = {"needs_you": None}

    async def approve(url, *, hosts, code=None, callback=None, page_code_re=None, done=None, on_state=None, **kw):
        seen.update(url=url, code=code, hosts=hosts)
        service = next(s for s, c in connect.CONNECTORS.items() if set(c.hosts) == set(hosts))
        if seen["needs_you"]:
            on_state("needs_you", seen["needs_you"])  # a 2FA page: the test approves it later, like the human
        else:
            on_state("approving", "Clicked “Authorize”")
            (local_sandbox.logins / service / "approved").touch()
        for _ in range(300):
            if done():
                return {"state": "approved", "tab": "t1"}
            await asyncio.sleep(0.1)
        return {"state": "timeout", "tab": "t1"}

    async def close_tab(target):
        return None

    async def whoami():
        return "tester"

    async def statuses(ids=None):  # the browser is signed in to every service
        return {"browser_online": True, "accounts": [{"id": i, "name": i, "status": "signed_in"} for i in ids or []]}

    from todd import accounts

    monkeypatch.setattr(accounts, "statuses", statuses)
    monkeypatch.setattr(cdp, "approve", approve)
    monkeypatch.setattr(cdp, "close_tab", close_tab)
    monkeypatch.setattr(infra, "github_whoami", whoami)
    return seen


def _ctx(agent_toolsets: list[str]) -> RunContext:
    ctx = RunContext(new_run("connect test"))
    try:  # the local sandbox stand-in runs commands in its own workspace
        os.makedirs(os.path.join(config.workspace_root, ctx.run_id), exist_ok=True)
    except OSError:
        pass
    ctx.agents["a_conn"] = SimpleNamespace(id="a_conn", name="Repo", toolsets=agent_toolsets, task=None)
    set_ctx(ctx)
    set_current_agent("a_conn")
    return ctx


def test_find_integrations_routes_to_connect_then_cli():
    vault.delete_secret("GITHUB_TOKEN")
    toolsets = {"github", "vercel", "sandbox", "browser"}
    r = integrations.assess("github", toolsets, {"github": "signed_in"})
    assert r["route"] == "connect", r  # not "plugin, ready" just because a toolset is named github
    assert 'cli_login("github")' in r["recommendation"] and "signed in" in r["recommendation"]
    assert "Don't create tokens" in r["recommendation"]
    assert "request_signins" in integrations.assess("github", toolsets, {"github": "signed_out"})["recommendation"]
    assert integrations.assess("vercel", toolsets)["route"] == "connect"
    vault.set_secret(connect.state_name("vercel"), "c25hcHNob3Q=")
    vault.set_secret("GITHUB_TOKEN", "ghp_connectedalready123")
    try:
        v = integrations.assess("vercel", toolsets)
        assert v["route"] == "cli" and 'cli("vercel"' in v["recommendation"]
        assert integrations.assess("github", toolsets)["route"] == "toolset"
        assert integrations.assess("supabase", toolsets)["route"] != "connect"  # no browser sign-in for it
    finally:
        vault.delete_secret(connect.state_name("vercel"))
        vault.delete_secret("GITHUB_TOKEN")


def test_cli_sign_ins_are_protected():
    assert vault.is_protected(connect.state_name("vercel")) and vault.is_protected("GITHUB_TOKEN")


def test_cli_login_github_puts_the_token_in_the_vault(loop, local_sandbox, fake_approval):
    async def go():
        _ctx(["sandbox"])
        r = await cl.cli_login.ainvoke({"service": "github"})
        assert r["connected"] is True and "git_push" in r["note"]
        assert fake_approval["code"] == "ABCD-1234" and fake_approval["url"] == "https://github.com/login/device"
        assert vault.get_secret("GITHUB_TOKEN") == "gho_fromthebrowser1234567890"
        assert "gho_" not in str(r)  # the credential never reached the agent
        assert not (local_sandbox.logins / "github").exists()  # the CLI's sign-in is gone from the sandbox
        again = await cl.cli_login.ainvoke({"service": "github"})
        assert "Already connected" in again["note"]
    loop.run_until_complete(go())


def test_connect_vercel_snapshot_and_cli_run(loop, local_sandbox, fake_approval):
    async def go():
        _ctx(["sandbox"])
        conn = connect.start("vercel")  # what the Accounts page does right after the human signs in
        await asyncio.wait_for(conn.done.wait(), 30)
        assert conn.state == "connected", conn.message
        assert fake_approval["code"] == "WXYZ-2345"  # from the approval URL, not the telemetry link before it
        first = vault.get_secret(connect.state_name("vercel"))
        assert first and connect.is_connected("vercel") and connect.status("vercel")["connected"]
        r = await sandbox_tools.cli.ainvoke({"service": "vercel", "command": "deploy --prod --yes"})
        assert r["exit_code"] == 0 and 'deploying deploy --prod --yes as {"session":"one"}' in r["output"]
        assert "__TODD" not in r["output"]
        assert vault.get_secret(connect.state_name("vercel")) != first  # the refreshed session was saved
        r = await sandbox_tools.cli.ainvoke({"service": "vercel", "command": "deploy"})
        assert '{"session":"two"}' in r["output"]
        with pytest.raises(ToolError, match="Blocked"):
            await sandbox_tools.cli.ainvoke({"service": "vercel", "command": "logout"})
        with pytest.raises(ToolError, match="cli_login"):
            await sandbox_tools.cli.ainvoke({"service": "netlify", "command": "status"})
        connect.disconnect("vercel")
        assert not connect.is_connected("vercel")
    loop.run_until_complete(go())


def test_cli_login_asks_the_human_once_when_the_page_needs_them(loop, local_sandbox, fake_approval):
    fake_approval["needs_you"] = "The page wants you to confirm it's you (2FA)"

    async def go():
        ctx = _ctx(["sandbox"])
        task = asyncio.create_task(cl.cli_login.ainvoke({"service": "github"}))
        it = None
        for _ in range(200):
            with session() as s:
                it = s.exec(select(Interaction).where(Interaction.run_id == ctx.run_id)).first()
            if it:
                break
            await asyncio.sleep(0.05)
        assert it and "2FA" in it.prompt and it.status == "pending"
        (local_sandbox.logins / "github" / "approved").touch()  # the human finishes the 2FA in the browser panel
        r = await asyncio.wait_for(task, 30)
        assert r["connected"] is True
        with session() as s:
            assert s.get(Interaction, it.id).status == "answered"  # closed for them, nothing to reply
    loop.run_until_complete(go())


def test_gh_tool_signs_in_per_command_and_hides_the_token(loop, local_sandbox):
    async def go():
        _ctx(["sandbox"])
        with pytest.raises(ToolError, match="cli_login"):
            await sandbox_tools.gh.ainvoke({"command": "repo view"})  # not connected yet
        vault.set_secret("GITHUB_TOKEN", "gho_vaulttoken1234567890")
        r = await sandbox_tools.cli.ainvoke({"service": "github", "command": "api user"})
        assert r["exit_code"] == 0 and "host=github.com" in r["output"] and "token=***" in r["output"]
        assert local_sandbox.calls[-1]["env"] == {"GH_TOKEN": "gho_vaulttoken1234567890"}  # only the token is redacted
        assert "gho_vaulttoken" not in local_sandbox.calls[-1]["cmd"]  # env only, never in the command
        for bad in ("auth token", "extension install evil/ext", "alias set x '!sh'", "api user --hostname evil.com"):
            with pytest.raises(ToolError):
                await sandbox_tools.gh.ainvoke({"command": bad})
    loop.run_until_complete(go())


# ------------------------------------------------------------------------------ real Chromium
def _cdp_up() -> bool:
    try:
        return httpx.get(f"http://{config.browser_cdp_host}:{config.browser_cdp_port}/json/version", timeout=1).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _cdp_up(), reason="no browser reachable over CDP")

PAGES = {
    # GitHub-style device activation: one box per character, then an Authorize button that's disabled at first.
    "/device": """<form action="/authorize"><h1>Device activation</h1>
      <p>Enter the code displayed on your device</p>
      <div>""" + "".join(f'<input type="text" maxlength="1" name="c{i}">' for i in range(8)) + """</div>
      <button type="button">Cancel</button> <button type="submit">Continue</button></form>""",
    "/authorize": """<h1>Authorize Test CLI</h1><form method="post" action="/approve">
      <button type="button" onclick="location='/denied'">Cancel</button>
      <button id="ok" type="submit" disabled>Authorize test-cli</button></form>
      <script>setTimeout(() => document.getElementById('ok').disabled = false, 1500)</script>""",
    "/approve": "<h1>Congratulations, you're all set!</h1>",
    "/denied": "<h1>Denied</h1>",
    "/twofa": """<h1>Two-factor authentication</h1><input autocomplete="one-time-code" name="app_otp">
      <button>Verify</button>""",
    "/oauth": """<h1>Allow Test CLI to access your account?</h1>
      <button onclick="location='http://localhost:59999/oauth/callback?code=abc123&state=s1'">Allow</button>""",
    "/showcode": """<h1>Did you just run test login?</h1><button onclick="document.body.innerHTML=
      '<p>Copy this code into your CLI</p><input readonly value=&quot;4/0AbCdEfGhIjKlMnOpQrStUvWxYz01&quot;>'">Yes, I did</button>""",
}


@pytest.fixture
def provider():
    hits: list[str] = []

    class H(BaseHTTPRequestHandler):
        def _page(self):
            u = urlparse(self.path)
            hits.append(self.path)
            body = PAGES.get(u.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((body or "not found").encode())

        do_GET = do_POST = _page

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://{os.getenv('TODD_TEST_PAGE_HOST', '127.0.0.1')}:{server.server_address[1]}"
    yield SimpleNamespace(base=base, hits=hits, host=urlparse(base).hostname)
    server.shutdown()


@live
def test_approve_fills_the_code_and_authorizes(loop, provider):
    async def go():
        states: list[str] = []
        r = await cdp.approve(provider.base + "/device", hosts=[provider.host], code="ABCD-1234",
                              done=lambda: any(h.startswith("/approve") for h in provider.hits),
                              on_state=lambda s, m: states.append(s), timeout=30)
        assert r["state"] == "approved", (provider.hits, states)
        authorize = next(h for h in provider.hits if h.startswith("/authorize"))
        assert "".join(v[0] for v in parse_qs(urlparse(authorize).query).values()) == "ABCD1234"
        assert "/denied" not in provider.hits and "needs_you" not in states  # never clicked Cancel
        await cdp.close_tab(r["tab"])
    loop.run_until_complete(go())


@live
def test_approve_hands_2fa_to_the_human(loop, provider):
    async def go():
        states: list[tuple[str, str]] = []
        r = await cdp.approve(provider.base + "/twofa", hosts=[provider.host], code="ABCD-1234",
                              done=lambda: bool(states) and states[-1][0] == "needs_you",
                              on_state=lambda s, m: states.append((s, m)), timeout=20)
        assert states[-1][0] == "needs_you" and "2FA" in states[-1][1]
        assert r["state"] == "approved"  # done() stands in for the human finishing in the tab
        assert not any(h.startswith("/approve") for h in provider.hits)
        await cdp.close_tab(r["tab"])
    loop.run_until_complete(go())


@live
def test_approve_catches_a_localhost_callback_and_reads_page_codes(loop, provider):
    async def go():
        r = await cdp.approve(provider.base + "/oauth", hosts=[provider.host], callback="http://localhost:59999/",
                              timeout=20)
        assert r["state"] == "approved" and r["callback"] == "http://localhost:59999/oauth/callback?code=abc123&state=s1"
        await cdp.close_tab(r["tab"])
        r = await cdp.approve(provider.base + "/showcode", hosts=[provider.host],
                              page_code_re=r"\b(4/[0-9A-Za-z_\-]{20,})", timeout=20)
        assert r["state"] == "approved" and r["page_code"] == "4/0AbCdEfGhIjKlMnOpQrStUvWxYz01"
        await cdp.close_tab(r["tab"])
    loop.run_until_complete(go())


PAGE = """<!doctype html><html><body>
<h1>Token created</h1>
<p>Make sure to copy your token now.</p>
<div id="host"></div>
<input id="pw" type="password" value="hunter2-do-not-read">
<pre>def hello():
    return 42</pre>
<script>
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  root.innerHTML = '<code id="tok">github_pat_11AAAAAAA0' + 'x'.repeat(70) + '</code>'
    + '<clipboard-copy value="github_pat_11AAAAAAA0' + 'x'.repeat(70) + '" aria-label="Copy token" tabindex="0">Copy</clipboard-copy>';
  console.error('boom from the page'); fetch('/missing.js');
</script>
</body></html>"""


@live
def test_browser_read_text_save_secret_and_console(loop, provider):
    from todd.tools import browser_direct as bd

    PAGES["/token"] = PAGE
    token = "github_pat_11AAAAAAA0" + "x" * 70

    async def go():
        ctx = _ctx(["browser"])
        await bd.browser_start.ainvoke({"why_not_api": "Testing text copy on a local page.",
                                        "start_url": provider.base + "/token"})
        try:
            page = await bd.browser_read_text.ainvoke({})
            assert "Make sure to copy your token now." in page
            assert token in page  # inside a shadow root, untruncated
            assert "def hello():\n    return 42" in page  # code keeps its formatting
            assert "hunter2" not in page  # password fields are never read
            h = bd._handles(ctx)["a_conn"]
            index = next(i for i, n in (await h.browser.get_selector_map()).items()
                         if (n.attributes or {}).get("aria-label") == "Copy token")
            with pytest.raises(ToolError):
                await bd.browser_save_secret.ainvoke({"index": index, "name": "GITHUB_TOKEN"})  # protected
            saved = await bd.browser_save_secret.ainvoke({"index": index, "name": "TEST_PAGE_TOKEN"})
            assert token not in saved and "TEST_PAGE_TOKEN" in saved
            assert vault.get_secret("TEST_PAGE_TOKEN") == token
            report = await bd.browser_console.ainvoke({"reload": True})
            assert "ERROR: boom from the page" in report, report
            assert "FAILED REQUEST: 404" in report and "missing.js" in report, report
        finally:
            vault.delete_secret("TEST_PAGE_TOKEN")
            await bd.release(ctx, "a_conn", success=True)
    loop.run_until_complete(go())
