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
SECRETS = ("EXPO_TOKEN", "ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY", "APPLE_TEAM_ID", "APPLE_TEAM_TYPE")


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
            with pytest.raises(ToolError, match="EXPO_TOKEN"):
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
        key_file = eas_sandbox.home / sandbox_tools.ASC_KEY_FILE
        assert not key_file.exists()  # removed after the command
        call = eas_sandbox.calls[-1]
        assert BODY[:40] not in call["cmd"] and "ABC123XYZ9" not in call["cmd"]
        # a failing command still removes the key
        r = await sandbox_tools.eas.ainvoke({"command": "fail"})
        assert r["exit_code"] == 7 and not key_file.exists()
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
    monkeypatch.delenv("EXPO_TOKEN", raising=False)
    vault.delete_secret("EXPO_TOKEN")
    for q in ("expo", "EAS", "iphone", "react native"):
        r = integrations.assess(q, {"sandbox"})
        assert r["service"] == "Expo (EAS)" and r["route"] == "needs_key", r
        assert "Settings → Vault" in r["recommendation"] and "eas(" in r["recommendation"]
    vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890abcdef")
    try:
        r = integrations.assess("expo", {"sandbox"})
        assert r["route"] == "cli" and "`eas(…)`" in r["recommendation"]
        assert "can't create a new app record" in (integrations._find("app store connect").browser_note or "")
    finally:
        vault.delete_secret("EXPO_TOKEN")
