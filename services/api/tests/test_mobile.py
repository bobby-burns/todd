"""The `mobile` toolset: EAS in the sandbox with keys handed over per command, App Store Connect / Google Play API
calls signed in the API process, approvals for anything that reaches people, and routing to it."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from todd import integrations, vault
from todd.config import config
from todd.runtime import RunContext, set_current_agent
from todd.sdk import ToolError, set_ctx
from todd.tools import infra, mobile, sandbox

from .helpers import new_run

NAMES = ("EXPO_TOKEN", "ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY", "APPLE_TEAM_ID",
         "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON")

# Stand-in for eas-cli: shows what it was given, including the key files, while they exist.
FAKE_EAS = r"""#!/bin/bash
if [ -f .no-cert ]; then  # what eas prints for an iOS build with no distribution certificate on EAS yet
  echo "Distribution Certificate is not validated for non-interactive builds."
  echo "Credentials are not set up. Run this command again in interactive mode."; exit 1
fi
echo "argv: $*"
echo "token: $EXPO_TOKEN"
echo "asc: $EXPO_ASC_KEY_ID $EXPO_ASC_ISSUER_ID team=$EXPO_APPLE_TEAM_ID"
if [ -n "$EXPO_ASC_API_KEY_PATH" ]; then
  echo "p8: $EXPO_ASC_API_KEY_PATH lines=$(wc -l < "$EXPO_ASC_API_KEY_PATH") dir=$(stat -c %a "$(dirname "$EXPO_ASC_API_KEY_PATH")")"
fi
if [ -n "$TODD_PLAY_KEY_PATH" ]; then
  echo "play: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["client_email"])' "$TODD_PLAY_KEY_PATH")"
fi
echo "novcs=${EAS_NO_VCS:-0} leftover=${TODD_ASC_P8:-none}"
"""


def _ec_pem() -> tuple[str, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode(), key


def _rsa_pem() -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode(), key


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture
def clean_vault():
    for n in NAMES:
        vault.delete_secret(n)
    yield
    for n in NAMES:
        vault.delete_secret(n)


@pytest.fixture
def local_sandbox(tmp_path, monkeypatch, clean_vault):
    bindir, work = tmp_path / "bin", tmp_path / "work"
    bindir.mkdir()
    work.mkdir()
    f = bindir / "eas"
    f.write_text(FAKE_EAS)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    calls: list[dict] = []

    async def exec_(cmd, cwd, timeout=300, env=None):
        calls.append({"cmd": cmd, "cwd": cwd, "env": dict(env or {})})
        os.makedirs(cwd, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", **(env or {})})
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 5)
        return {"exit_code": proc.returncode, "output": out.decode(), "timed_out": False}

    monkeypatch.setattr(sandbox, "exec_", exec_)
    monkeypatch.setattr(config, "workspace_root", str(work))
    return SimpleNamespace(calls=calls, work=work)


def _ctx(approve: bool | None = None) -> SimpleNamespace:
    """A run context whose approvals are answered with `approve` (None: fail if one is asked)."""
    ctx = RunContext(new_run("mobile test"))
    asked: list[str] = []

    async def request_approval(action, agent=None, data=None, kind="approval"):
        asked.append(action)
        if approve is None:
            raise AssertionError(f"unexpected approval request: {action}")
        return approve, "" if approve else "not yet"

    ctx.request_approval = request_approval  # type: ignore[method-assign]
    set_ctx(ctx)
    set_current_agent("a_mobile")
    return SimpleNamespace(ctx=ctx, asked=asked)


def test_pem_survives_a_one_line_paste():
    text, key = _ec_pem()
    one_line = text.replace("\n", "")  # what a single-line input field does to it
    restored = mobile.pem(one_line)
    loaded = serialization.load_pem_private_key(restored.encode(), password=None)
    assert loaded.private_numbers() == key.private_numbers()  # type: ignore[union-attr]
    assert mobile.pem(text) == restored
    with pytest.raises(ToolError):
        mobile.pem("-----BEGIN PRIVATE KEY----------END PRIVATE KEY-----")


def test_asc_token_is_a_valid_es256_jwt(clean_vault):
    with pytest.raises(ToolError, match="ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY"):
        mobile.asc_token()
    text, key = _ec_pem()
    vault.set_secret("ASC_KEY_ID", "ABC123DEFG")
    vault.set_secret("ASC_ISSUER_ID", "57246542-96fe-1a63-e053-0824d011072a")
    vault.set_secret("ASC_PRIVATE_KEY", text.replace("\n", " "))
    token = mobile.asc_token(now=1_700_000_000)
    h, c, sig = token.split(".")
    assert json.loads(_unb64(h)) == {"alg": "ES256", "kid": "ABC123DEFG", "typ": "JWT"}
    claims = json.loads(_unb64(c))
    assert claims["iss"] == "57246542-96fe-1a63-e053-0824d011072a" and claims["aud"] == "appstoreconnect-v1"
    assert claims["exp"] - claims["iat"] <= 1200  # Apple rejects tokens that live longer than 20 minutes
    raw = _unb64(sig)
    assert len(raw) == 64  # raw r||s
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))


def test_store_keys_are_protected(clean_vault):
    vault.set_secret("ASC_PRIVATE_KEY", "k")
    vault.set_secret("EXPO_TOKEN", "t")
    for name in ("EXPO_TOKEN", "ASC_PRIVATE_KEY", "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"):
        assert vault.is_protected(name)
        with pytest.raises(ToolError, match="protected"):
            infra.resolve_secrets(f"{{{{secret:{name}}}}}")
    assert not vault.is_protected("ASC_KEY_ID")


def test_eas_needs_a_token_and_blocks_sign_in_commands(loop, local_sandbox):
    async def go():
        _ctx()
        with pytest.raises(ToolError, match="EXPO_TOKEN"):
            await mobile.eas.ainvoke({"command": "build:list"})
        vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890")
        for bad in ("login", "logout", "account:login -u me", "credentials -p ios", "credentials:configure-build"):
            with pytest.raises(ToolError, match="isn't available"):
                await mobile.eas.ainvoke({"command": bad})
        with pytest.raises(ToolError, match="escapes"):
            await mobile.eas.ainvoke({"command": "build:list", "path": "../../etc"})
        assert local_sandbox.calls == []
    loop.run_until_complete(go())


def test_eas_hands_keys_over_for_one_command(loop, local_sandbox):
    async def go():
        _ctx()
        text, _ = _ec_pem()
        vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890")
        vault.set_secret("ASC_KEY_ID", "ABC123DEFG")
        vault.set_secret("ASC_ISSUER_ID", "issuer-0000-1111")
        vault.set_secret("ASC_PRIVATE_KEY", text.replace("\n", ""))
        vault.set_secret("APPLE_TEAM_ID", "TEAM123456")
        vault.set_secret("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "ci@p.iam.gserviceaccount.com",
                                                                         "private_key": "x"}))
        r = await mobile.eas.ainvoke({"command": "eas build --platform ios --profile production --non-interactive --no-wait",
                                      "path": "app"})
        out = r["output"]
        assert r["exit_code"] == 0, out
        assert "argv: build --platform ios --profile production --non-interactive --no-wait" in out
        assert "token: ***" in out and "expo_tok_" not in out
        assert "asc: ABC123DEFG issuer-0000-1111 team=TEAM123456" in out
        p8 = next(line for line in out.splitlines() if line.startswith("p8: ")).split()[1]
        assert "lines=5" in out or "lines=4" in out, out  # a real multi-line PEM again, not the one-line paste
        assert "dir=700" in out and not os.path.exists(p8)  # private, and gone after the command
        assert "play: ci@p.iam.gserviceaccount.com" in out
        assert "leftover=none" in out and "novcs=1" in out  # not a git repo: EAS archives the directory
        call = local_sandbox.calls[-1]
        assert call["cwd"].endswith("/app") and "expo_tok_" not in call["cmd"] and "BEGIN" not in call["cmd"]
    loop.run_until_complete(go())


def test_eas_says_how_to_create_the_first_ios_certificate(loop, local_sandbox):
    async def go():
        ctx = _ctx().ctx
        vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890")
        app = local_sandbox.work / ctx.run_id / "app"
        app.mkdir(parents=True)
        (app / ".no-cert").touch()
        r = await mobile.eas.ainvoke({"command": "build -p ios --profile production --non-interactive", "path": "app"})
        assert r["exit_code"] == 1
        assert f"docker compose exec -it -w {app} sandbox" in r["next_step"] and "eas logout" in r["next_step"]
        (app / ".no-cert").unlink()
        assert "next_step" not in await mobile.eas.ainvoke({"command": "build:list", "path": "app"})
    loop.run_until_complete(go())


@pytest.mark.parametrize("command,needs", [
    ("submit --platform ios --latest --non-interactive", True),
    ("build --platform all --auto-submit --non-interactive", True),
    ("update --branch production --message fix", True),
    ("channel:rollout production", True),
    ("deploy --prod", True),
    ("metadata:push", True),
    ("build --platform android --profile preview --no-wait", False),
    ("build:view abc --json", False),
    ("update:list --json", False),
    ("deploy", False),
])
def test_eas_asks_before_anything_reaches_people(command, needs):
    assert bool(mobile.eas_publishes(command.split())) is needs


def test_eas_submit_waits_for_approval(loop, local_sandbox):
    async def go():
        vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890")
        t = _ctx(approve=False)
        with pytest.raises(ToolError, match="didn't approve"):
            await mobile.eas.ainvoke({"command": "submit --platform android --latest --non-interactive"})
        assert t.asked and local_sandbox.calls == []  # denied: nothing ran
        t = _ctx(approve=True)
        r = await mobile.eas.ainvoke({"command": "submit --platform android --latest --non-interactive"})
        assert r["exit_code"] == 0 and "argv: submit" in r["output"] and len(t.asked) == 1
    loop.run_until_complete(go())


@pytest.fixture
def google(clean_vault, monkeypatch):
    """A local stand-in for Google's token endpoint and the Play Developer API."""
    text, key = _rsa_pem()
    seen: dict = {"requests": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            if self.path == "/token":
                form = parse_qs(body.decode())
                h, c, sig = form["assertion"][0].split(".")
                key.public_key().verify(_unb64(sig), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
                seen["claims"] = json.loads(_unb64(c))
                return self._send(200, {"access_token": "ya29.fake", "expires_in": 3600})
            seen["requests"].append(("POST", self.path, self.headers.get("authorization")))
            return self._send(200, {"id": "edit1"})

        def do_GET(self):
            seen["requests"].append(("GET", self.path, self.headers.get("authorization")))
            self._send(200, {"tracks": [{"track": "internal"}]})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    vault.set_secret("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", json.dumps(
        {"type": "service_account", "client_email": "todd@proj.iam.gserviceaccount.com", "private_key": text,
         "token_uri": base + "/token"}))
    monkeypatch.setattr(mobile, "PLAY_API", base + "/androidpublisher/v3/")
    mobile._play_token.clear()
    yield seen
    srv.shutdown()
    mobile._play_token.clear()


def test_google_play_signs_in_with_the_service_account_and_asks_before_commit(loop, google):
    async def go():
        _ctx()
        r = await mobile.google_play.ainvoke({"method": "GET", "path": "applications/com.example.app/edits/e1/tracks"})
        assert r["ok"] and "internal" in r["body"]
        assert google["claims"]["iss"] == "todd@proj.iam.gserviceaccount.com"
        assert google["claims"]["scope"] == mobile.PLAY_SCOPE
        assert google["requests"][-1] == ("GET", "/androidpublisher/v3/applications/com.example.app/edits/e1/tracks",
                                          "Bearer ya29.fake")
        with pytest.raises(ToolError, match="API path"):
            await mobile.google_play.ainvoke({"method": "GET", "path": "https://evil.example/x"})
        t = _ctx(approve=False)
        with pytest.raises(ToolError, match="didn't approve"):
            await mobile.google_play.ainvoke({"method": "POST", "path": "applications/com.example.app/edits/e1:commit"})
        with pytest.raises(ToolError, match="didn't approve"):
            await mobile.google_play.ainvoke({"method": "POST", "path": "applications/com.example.app/reviews/r1:reply",
                                              "body": {"replyText": "Thanks!"}})
        with pytest.raises(ToolError, match="didn't approve"):  # a query string in the path doesn't skip it
            await mobile.google_play.ainvoke({"method": "POST",
                                              "path": "applications/com.example.app/edits/e1:commit?x=1"})
        assert len(t.asked) == 3 and all(":commit" not in p and ":reply" not in p for _, p, _ in google["requests"])
        t = _ctx(approve=True)
        r = await mobile.google_play.ainvoke({"method": "POST", "path": "applications/com.example.app/edits/e1:commit"})
        assert r["ok"] and google["requests"][-1][1].endswith("/edits/e1:commit") and len(t.asked) == 1
    loop.run_until_complete(go())


def test_app_store_connect_asks_before_submitting_for_review(loop, clean_vault, monkeypatch):
    sent: list = []

    class FakeClient:
        def __init__(self, **kw):
            self.base = kw.get("base_url")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, path, params=None, json=None, headers=None):
            import httpx

            sent.append((method, self.base + path, headers["Authorization"][:7]))
            return httpx.Response(200, json={"data": []}, request=httpx.Request(method, self.base + path))

    monkeypatch.setattr(mobile.httpx, "AsyncClient", FakeClient)
    text, _ = _ec_pem()
    for n, v in (("ASC_KEY_ID", "K1"), ("ASC_ISSUER_ID", "I1"), ("ASC_PRIVATE_KEY", text)):
        vault.set_secret(n, v)

    async def go():
        _ctx()
        r = await mobile.app_store_connect.ainvoke({"method": "GET", "path": "v1/apps",
                                                    "params": {"filter[bundleId]": "com.example.app"}})
        assert r["ok"] and sent[-1] == ("GET", "https://api.appstoreconnect.apple.com/v1/apps", "Bearer ")
        t = _ctx(approve=False)
        with pytest.raises(ToolError, match="didn't approve"):
            await mobile.app_store_connect.ainvoke({"method": "POST", "path": "/v1/reviewSubmissions",
                                                    "body": {"data": {"type": "reviewSubmissions"}}})
        with pytest.raises(ToolError, match="didn't approve"):
            await mobile.app_store_connect.ainvoke({"method": "PATCH", "path": "/v1/betaGroups/g1",
                                                    "body": {"data": {"attributes": {"publicLinkEnabled": True}}}})
        assert len(t.asked) == 2 and len(sent) == 1
        await mobile.app_store_connect.ainvoke({"method": "PATCH", "path": "/v1/apps/1/appInfos",
                                                "body": {"data": {"attributes": {"name": "x"}}}})  # a draft edit
        assert len(sent) == 2 and len(t.asked) == 2
    loop.run_until_complete(go())


def test_find_integrations_routes_mobile_work(clean_vault):
    r = integrations.assess("ios app", {"sandbox"})
    assert r["service"] == "Expo (EAS)" and r["route"] == "browser" and "EXPO_TOKEN" in r["recommendation"]
    vault.set_secret("EXPO_TOKEN", "expo_tok_1234567890")
    assert integrations.assess("expo", {"sandbox", "mobile"})["route"] == "toolset"
    r = integrations.assess("expo", {"sandbox"})  # keys ready, but only the mobile tools can use them
    assert r["route"] == "toolset_needed" and "`mobile`" in r["recommendation"]
    assert integrations.assess("testflight", {"mobile"})["route"] == "browser"  # no App Store Connect key yet
    for n, v in (("ASC_KEY_ID", "K"), ("ASC_ISSUER_ID", "I"), ("ASC_PRIVATE_KEY", "P")):
        vault.set_secret(n, v)
    assert integrations.assess("app store connect", {"mobile"})["route"] == "toolset"
    assert integrations.assess("play store", {"mobile"})["service"] == "Google Play Console"
