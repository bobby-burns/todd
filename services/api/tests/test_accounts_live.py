"""Accounts against a real Chromium over CDP (skipped unless the browser is reachable).
Run with BROWSER_CDP_HOST/BROWSER_CDP_PORT pointing at the browser, e.g. inside compose."""

from __future__ import annotations

import asyncio
import time

import pytest

from todd import accounts, cdp, settings


def browser_up() -> bool:
    return asyncio.run(cdp.online())


pytestmark = pytest.mark.skipif(not browser_up(), reason="browser not reachable over CDP")


async def _set_cookie(name: str, domain: str, value: str = "x" * 20) -> None:
    async with cdp.CDP() as c:
        await c.send("Storage.setCookies", {"cookies": [{"name": name, "value": value, "domain": domain, "path": "/",
                                                         "secure": True, "expires": time.time() + 3600}]})


def test_cookie_detection_via_and_signout(loop):
    async def go():
        await accounts.sign_out("github")
        await accounts.sign_out("google")
        st = {a["id"]: a for a in (await accounts.statuses(["github", "google", "firebase", "x"]))["accounts"]}
        assert st["github"]["status"] == "signed_out" and st["firebase"]["status"] == "signed_out"
        await _set_cookie("user_session", ".github.com")
        await _set_cookie("SID", ".google.com")
        accounts._cookie_cache = None
        st = {a["id"]: a for a in (await accounts.statuses(["github", "google", "firebase"]))["accounts"]}
        assert st["github"]["status"] == "signed_in" and st["github"]["source"] == "session cookie"
        assert st["firebase"]["status"] == "signed_in" and st["firebase"]["source"].startswith("via")
        assert st["github"]["expires"] and st["github"]["expires"] > time.time()
        assert await accounts.sign_out("github") >= 1
        st = {a["id"]: a for a in (await accounts.statuses(["github"]))["accounts"]}
        assert st["github"]["status"] == "signed_out"
    loop.run_until_complete(go())


def test_probe_detects_login_redirect(loop):
    async def go():
        settings.update({"accounts_custom": [
            {"id": "custom_local", "name": "Local", "login": "http://127.0.0.1:8765/login.html",
             "check": "http://127.0.0.1:8765/page.html", "domains": ["127.0.0.1"], "cookies": []},
            {"id": "custom_redirect", "name": "Redir", "login": "http://127.0.0.1:8765/login.html",
             "check": "http://127.0.0.1:8765/redirect.html", "domains": ["127.0.0.1"], "cookies": []}]})
        ok = await accounts.verify("custom_local")
        assert ok["signed_in"] is True, ok
        bad = await accounts.verify("custom_redirect")
        assert bad["signed_in"] is False and "login" in bad["final_url"], bad
        settings.update({"accounts_custom": [*settings.get("accounts_custom"),
            {"id": "custom_spa", "name": "Spa", "login": "http://127.0.0.1:8765/spa_login.html",
             "check": "http://127.0.0.1:8765/spa_login.html", "domains": ["127.0.0.1"], "cookies": []},
            {"id": "custom_down", "name": "Down", "login": "http://127.0.0.1:9/", "check": "http://127.0.0.1:9/",
             "domains": ["127.0.0.1"], "cookies": []}]})
        spa = await accounts.verify("custom_spa")
        assert spa["signed_in"] is False, spa  # password field on screen = signed out, even without a redirect
        down = await accounts.verify("custom_down")
        assert down["signed_in"] is None, down  # unreachable site: unknown, not "signed in"
        st = {a["id"]: a for a in (await accounts.statuses(["custom_local", "custom_redirect"]))["accounts"]}
        assert st["custom_local"]["status"] == "signed_in" and st["custom_local"]["source"] == "verified by visit"
        assert st["custom_redirect"]["status"] == "signed_out"
    loop.run_until_complete(go())


def test_check_accounts_tool(loop):
    async def go():
        from todd.runtime import RunContext
        from todd.sdk import set_ctx
        from .helpers import new_run

        set_ctx(RunContext(new_run("acct")))
        r = await accounts.check_accounts.ainvoke({"services": ["GitHub", "firebase", "stripe.com", "myspace"],
                                                   "verify_unknown": False})
        ids = [a["id"] for a in r["accounts"]]
        assert ids == ["github", "firebase", "stripe"] and r["not_in_catalog"] == ["myspace"]
    loop.run_until_complete(go())
