"""Mobile apps with Expo: the `eas` tool signs EAS in with EXPO_TOKEN, hands EAS the App Store Connect key for the
length of one command, keeps both out of the command text and the output, and find_integrations routes to it."""

from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace

import pytest

from todd import integrations, vault
from todd.config import config
from todd.sdk import ToolError
from todd.tools import infra, sandbox, sandbox_tools

from .test_connect import _ctx

# A stand-in for eas-cli that reports what it was given (never the secrets themselves).
FAKE_EAS = r"""#!/bin/bash
echo "eas $*"
echo "token=${EXPO_TOKEN:+set} tok=$EXPO_TOKEN"
echo "kid=$EXPO_ASC_KEY_ID iss=$EXPO_ASC_ISSUER_ID team=$EXPO_APPLE_TEAM_ID type=$EXPO_APPLE_TEAM_TYPE"
echo "raw=${TODD_ASC_KEY:-unset}"
if [ -n "$EXPO_ASC_API_KEY_PATH" ]; then
  echo "keyfile=$EXPO_ASC_API_KEY_PATH mode=$(stat -c %a "$EXPO_ASC_API_KEY_PATH") lines=$(wc -l < "$EXPO_ASC_API_KEY_PATH")"
  head -1 "$EXPO_ASC_API_KEY_PATH"
fi
[ "$1" = "fail" ] && exit 7
exit 0
"""
BODY = "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg" + "A" * 120
PEM = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(BODY[i:i + 64] for i in range(0, len(BODY), 64)) + \
      "\n-----END PRIVATE KEY-----\n"
SECRETS = ("EXPO_TOKEN", "CLI_STATE_EXPO", "ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY", "APPLE_TEAM_ID", "APPLE_TEAM_TYPE")


@pytest.fixture
def eas_sandbox(tmp_path, monkeypatch):
    bindir, work, home = tmp_path / "bin", tmp_path / "work", tmp_path / "home"
    for d in (bindir, work, home):
        d.mkdir()
    f = bindir / "eas"
    f.write_text(FAKE_EAS)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    calls: list[dict] = []

    async def exec_(cmd, cwd, timeout=300, env=None):
        calls.append({"cmd": cmd, "cwd": cwd, "env": dict(env or {})})
        os.makedirs(cwd, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}", **(env or {})})
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 5)
        return {"exit_code": proc.returncode, "output": out.decode(), "timed_out": False}

    monkeypatch.setattr(sandbox, "exec_", exec_)
    monkeypatch.setattr(config, "workspace_root", str(work))
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
        vault.delete_secret(k)
    yield SimpleNamespace(calls=calls, home=home)
    for k in SECRETS:
        vault.delete_secret(k)


def test_normalize_pem_accepts_the_ways_a_key_gets_pasted():
    import base64

    one_line = PEM.replace("\n", " ")  # a one-line input turns newlines into spaces
    for raw in (PEM, one_line, BODY, base64.b64encode(PEM.encode()).decode(), "  " + PEM + "  "):
        assert sandbox_tools.normalize_pem(raw) == PEM
    with pytest.raises(ToolError, match="doesn't look like"):
        sandbox_tools.normalize_pem("not a key!")


def test_eas_needs_a_token_and_blocks_login_and_interactive_commands(loop, eas_sandbox):
    async def go():
        _ctx(["sandbox"])
        with pytest.raises(ToolError, match="EXPO_TOKEN"):
            await sandbox_tools.eas.ainvoke({"command": "whoami"})
        vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890abcdef")
        for bad in ("login", "logout -s", "account:login"):
            with pytest.raises(ToolError, match="signs EAS in"):
                await sandbox_tools.eas.ainvoke({"command": bad})
        with pytest.raises(ToolError, match="person at a terminal"):
            await sandbox_tools.eas.ainvoke({"command": "credentials -p ios"})
        assert not eas_sandbox.calls  # nothing ran
    loop.run_until_complete(go())


def test_eas_signs_in_per_command_and_hides_the_token(loop, eas_sandbox):
    async def go():
        ctx = _ctx(["sandbox"])
        vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890abcdef")
        r = await sandbox_tools.eas.ainvoke({"command": "build -p ios --non-interactive --no-wait", "path": "app"})
        assert r["exit_code"] == 0, r
        assert "eas build -p ios --non-interactive --no-wait" in r["output"]
        assert "token=set" in r["output"] and "expo_tok_" not in r["output"]  # redacted
        assert "keyfile=" not in r["output"] and "No App Store Connect API key" in r["apple"]
        call = eas_sandbox.calls[-1]
        assert call["env"] == {"EXPO_TOKEN": "expo_tok_1234567890abcdef"} and "expo_tok_" not in call["cmd"]
        assert call["cwd"] == os.path.join(config.workspace_root, ctx.run_id, "app")
        via_cli = await sandbox_tools.cli.ainvoke({"service": "expo", "command": "fail"})
        assert via_cli["exit_code"] == 7 and "eas fail" in via_cli["output"]
    loop.run_until_complete(go())


def test_eas_gets_the_app_store_connect_key_only_for_the_command(loop, eas_sandbox):
    async def go():
        _ctx(["sandbox"])
        vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890abcdef")
        vault.set_secret("ASC_KEY_ID", "ABC123XYZ9")
        vault.set_secret("ASC_ISSUER_ID", "69a6de70-aaaa-bbbb-cccc-123456789012")
        vault.set_secret("ASC_PRIVATE_KEY", PEM.replace("\n", " "))  # pasted into the one-line field
        vault.set_secret("APPLE_TEAM_ID", "TEAM12345")
        r = await sandbox_tools.eas.ainvoke({"command": "submit -p ios --latest --non-interactive"})
        out = r["output"]
        assert r["exit_code"] == 0 and "passed to EAS" in r["apple"]
        assert "raw=unset" in out  # the key itself isn't left in the CLI's environment
        assert "mode=600" in out and "-----BEGIN PRIVATE KEY-----" in out
        assert f"lines={PEM.count(chr(10))}" in out  # rewrapped to a real PEM
        assert "type=INDIVIDUAL" in out
        assert vault.get_secret("ASC_PRIVATE_KEY") == PEM.strip()  # stored as EAS sees it, so scrubbing matches
        key_file = eas_sandbox.home / sandbox_tools.ASC_KEY_FILE
        assert not key_file.exists()  # removed after the command
        call = eas_sandbox.calls[-1]
        assert BODY[:40] not in call["cmd"] and "ABC123XYZ9" not in call["cmd"]
        # a failing command still removes the key, and a key left by a killed command is removed first
        r = await sandbox_tools.eas.ainvoke({"command": "fail"})
        assert r["exit_code"] == 7 and not key_file.exists()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text("stale")
        vault.delete_secret("ASC_PRIVATE_KEY")
        r = await sandbox_tools.eas.ainvoke({"command": "whoami"})
        assert "keyfile=" not in r["output"] and not key_file.exists()
    loop.run_until_complete(go())


def test_expo_token_stays_inside_the_eas_tool():
    vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890abcdef")
    try:
        assert vault.is_protected("EXPO_TOKEN")
        with pytest.raises(ToolError, match="protected"):
            infra.resolve_secrets("{{secret:EXPO_TOKEN}}")
    finally:
        vault.delete_secret("EXPO_TOKEN")


def test_find_integrations_routes_mobile_work_to_eas(monkeypatch):
    from todd import connect

    monkeypatch.delenv("EXPO_TOKEN", raising=False)
    vault.delete_secret("EXPO_TOKEN")
    vault.delete_secret(connect.state_name("expo"))
    for q in ("expo", "EAS", "iphone", "react native"):
        r = integrations.assess(q, {"sandbox"}, {"expo": "signed_in"})
        assert r["service"] == "Expo (EAS)" and r["route"] == "connect", r  # browser sign-in first, no token to copy
        assert 'cli_login("expo")' in r["recommendation"] and "eas(…)" in r["recommendation"]
    for secret in ("EXPO_TOKEN", connect.state_name("expo")):
        vault.set_secret(secret, "expo_tok_1234567890abcdef")
        try:
            r = integrations.assess("expo", {"sandbox"})
            assert r["route"] == "cli" and "`eas(…)`" in r["recommendation"]
        finally:
            vault.delete_secret(secret)
    assert "can't create a new app record" in (integrations._find("app store connect").browser_note or "")


# eas-cli's browser login, as a stand-in: prints the expo.dev link with a random localhost port, waits for the redirect
# there, and keeps the session in $HOME/.expo like the real one.
FAKE_EAS_LOGIN = r"""#!/usr/bin/env python3
import http.server, json, os, socketserver, sys
args, state = sys.argv[1:], os.path.join(os.environ["HOME"], ".expo", "state.json")
if args[:1] == ["login"]:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = "code=abc" in self.path and "state=s1" in self.path
            if ok:
                os.makedirs(os.path.dirname(state), exist_ok=True)
                json.dump({"auth": {"sessionSecret": "sess-from-browser"}}, open(state, "w"))
            self.send_response(302)
            self.send_header("Location", "https://expo.dev/oauth/expo-cli?result=" + ("success" if ok else "error"))
            self.end_headers()
        def log_message(self, *a):
            pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    print("Waiting for browser login...")
    print("If your browser doesn't automatically open, visit this link to log in: https://expo.dev/login?"
          f"client_id=eas-cli&redirect_uri=http://localhost:{port}/auth/callback&response_type=code&state=s1", flush=True)
    srv.handle_request()
    print("Logged in")
    sys.exit(0 if os.path.exists(state) else 1)
print("eas " + " ".join(args))
print("session=" + (json.load(open(state))["auth"]["sessionSecret"] if os.path.exists(state) else "none"))
print("token=" + ("set" if os.environ.get("EXPO_TOKEN") else "unset"))
print("keyfile=" + os.environ.get("EXPO_ASC_API_KEY_PATH", "none"))
"""


def test_expo_signs_in_with_the_browser_session_then_eas_uses_it(loop, eas_sandbox, tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from todd import cdp, connect

    f = tmp_path / "bin" / "eas"
    f.write_text(FAKE_EAS_LOGIN)
    monkeypatch.setenv("TODD_CLI_LOGIN_DIR", str(tmp_path / "logins"))
    vault.delete_secret(connect.state_name("expo"))
    connect._CONNECTIONS.clear()
    seen: dict = {}

    async def approve(url, *, hosts, code=None, callback=None, **kw):
        # what Todd's browser does: approve on expo.dev, catch the redirect to the CLI's localhost port
        seen.update(url=url, hosts=hosts, callback=callback)
        redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]
        return {"state": "approved", "callback": f"{redirect}?code=abc&state=s1", "tab": "t1"}

    async def close_tab(target):
        return None

    monkeypatch.setattr(cdp, "approve", approve)
    monkeypatch.setattr(cdp, "close_tab", close_tab)

    async def go():
        _ctx(["sandbox"])
        conn = connect.start("expo")
        await asyncio.wait_for(conn.done.wait(), 30)
        assert conn.state == "connected", conn.message
        assert seen["url"].startswith("https://expo.dev/login?") and seen["hosts"] == ["expo.dev"]
        assert seen["callback"] == "http://localhost:*/auth/callback"
        assert connect.is_connected("expo") and not vault.get_secret("EXPO_TOKEN")
        vault.set_secret("ASC_KEY_ID", "ABC123XYZ9")
        vault.set_secret("ASC_ISSUER_ID", "69a6de70-aaaa-bbbb-cccc-123456789012")
        vault.set_secret("ASC_PRIVATE_KEY", PEM)
        r = await sandbox_tools.eas.ainvoke({"command": "whoami"})
        assert r["exit_code"] == 0, r
        assert "session=sess-from-browser" in r["output"] and "token=unset" in r["output"]
        assert f"keyfile={eas_sandbox.home}/{sandbox_tools.ASC_KEY_FILE}" in r["output"]  # real home, not the CLI's
        assert not (eas_sandbox.home / sandbox_tools.ASC_KEY_FILE).exists()
        with pytest.raises(ToolError, match="signs EAS in"):
            await sandbox_tools.eas.ainvoke({"command": "logout"})
        connect.disconnect("expo")
        with pytest.raises(ToolError, match="cli_login"):
            await sandbox_tools.eas.ainvoke({"command": "whoami"})
    loop.run_until_complete(go())


# ------------------------------------------------------------------ App Store Connect key, set up in the browser
def test_app_store_connect_keys_are_set_up_in_the_browser(monkeypatch):
    for n in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"):
        monkeypatch.delenv(n, raising=False)
        vault.delete_secret(n)
    r = integrations.assess("app store connect", {"sandbox", "browser"})
    assert r["route"] == "setup", r
    rec = r["recommendation"]
    assert "Setup: App Store Connect key" in rec and "browser_save_download" in rec and "ASC_PRIVATE_KEY" in rec
    assert "request_approval" in rec  # accepting Apple's API terms isn't the agent's call
    assert [i.id for i in integrations.setup_pending(["app_store_connect", "github"])] == ["app_store_connect"]
    for n in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"):
        vault.set_secret(n, "x")
    try:
        assert integrations.assess("app store connect", {"sandbox"})["route"] == "api"
        assert integrations.setup_pending(["app_store_connect"]) == []
    finally:
        for n in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"):
            vault.delete_secret(n)


class _FakePage:
    """Just enough of browser-use's session for page_capture: a Download button that makes a .p8 file."""

    def __init__(self, text: str | None, name: str = "AuthKey_ABC123XYZ9.p8"):
        self.hooked = self.clicked = False
        self.text, self.name, self.dl = text, name, None
        page = self

        async def evaluate(params, session_id=None):
            from todd.tools import page_capture as pc

            expr = params["expression"]
            if expr == pc.DOWNLOAD_HOOK_JS:
                page.hooked = True
                return {"result": {}}
            if expr == pc.TAKE_JS:  # read, clear and unhook in one step
                import json as _json
                d, page.dl = page.dl, None
                if d:
                    page.hooked = False
                return {"result": {"value": _json.dumps(d) if d else None}}
            if expr == pc.UNHOOK_JS:
                page.hooked = False
                return {"result": {}}
            return {"result": {"objectId": "body"}}

        async def call(params, session_id=None):
            assert page.hooked, "the hook must be in place before the click"
            page.clicked = True
            if page.text is not None:
                page.dl = {"name": page.name, "text": page.text}
            return {"result": {}}

        async def resolve(params, session_id=None):
            return {"object": {"objectId": "btn"}}

        send = SimpleNamespace(Runtime=SimpleNamespace(evaluate=evaluate, callFunctionOn=call),
                               DOM=SimpleNamespace(resolveNode=resolve))
        self.session = SimpleNamespace(cdp_client=SimpleNamespace(send=send), session_id="s1")

    async def get_element_by_index(self, index):
        return SimpleNamespace(backend_node_id=index) if index == 7 else None

    async def cdp_client_for_node(self, node):
        return self.session


def test_save_download_puts_the_key_file_in_the_vault(loop):
    from todd.tools import page_capture

    async def go():
        page = _FakePage(PEM)
        name, text = await page_capture.capture_download(page, 7)
        assert page.clicked and name == "AuthKey_ABC123XYZ9.p8" and text == PEM
        assert page.dl is None and not page.hooked  # nothing left in the page, downloads work normally again
        page_capture.check_name("ASC_PRIVATE_KEY")
        msg = page_capture.save_file("ASC_PRIVATE_KEY", name, text)
        assert "AuthKey_ABC123XYZ9.p8" in msg and "BEGIN" not in msg  # the key itself never comes back
        assert vault.get_secret("ASC_PRIVATE_KEY") == PEM.strip()
        assert sandbox_tools.normalize_pem(vault.get_secret("ASC_PRIVATE_KEY")) == PEM
        with pytest.raises(ToolError, match="No element"):
            await page_capture.capture_download(_FakePage(PEM), 3)
        idle = _FakePage(None)
        with pytest.raises(ToolError, match="No download was caught"):
            await page_capture.capture_download(idle, 7, timeout=1)
        assert not idle.hooked  # given up: hook removed
        with pytest.raises(ToolError, match="isn't text"):
            page_capture.save_file("X_FILE", "a.bin", "��")
        with pytest.raises(ToolError, match="larger"):
            page_capture.save_file("X_FILE", "big.txt", "a" * (page_capture.MAX_FILE_BYTES + 1))
        for bad in ("EXPO_TOKEN", "GITHUB_TOKEN", "lower_case"):
            with pytest.raises(ToolError):
                page_capture.check_name(bad)
        vault.delete_secret("ASC_PRIVATE_KEY")
    loop.run_until_complete(go())
