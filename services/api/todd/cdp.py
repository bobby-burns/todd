"""Minimal Chrome DevTools Protocol client for account management on the shared browser
(read session cookies, open login tabs, sign out, probe a page for a login redirect)."""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import httpx
from websockets.asyncio.client import connect

from .agents.browser import cdp_url


class CDPError(Exception):
    pass


class CDP:
    """Browser-level CDP session. Usage: `async with CDP() as c: await c.send("Target.getTargets")`."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._ws = None

    async def __aenter__(self) -> "CDP":
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                info = (await c.get(f"{cdp_url()}/json/version")).json()
        except Exception as e:  # noqa: BLE001
            raise CDPError(f"browser not reachable at {cdp_url()}: {e}") from e
        self._ws = await connect(info["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024, open_timeout=10)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15,
                   session_id: str | None = None) -> dict[str, Any]:
        assert self._ws is not None
        msg_id = next(self._ids)
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self._ws.send(json.dumps(msg))

        async def wait() -> dict[str, Any]:
            while True:
                data = json.loads(await self._ws.recv())
                if data.get("id") == msg_id:
                    if "error" in data:
                        raise CDPError(f"{method}: {data['error'].get('message')}")
                    return data.get("result", {})

        return await asyncio.wait_for(wait(), timeout)


async def online() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"{cdp_url()}/json/version")).status_code == 200
    except Exception:
        return False


async def get_cookies() -> list[dict[str, Any]]:
    async with CDP() as c:
        return (await c.send("Storage.getCookies")).get("cookies", [])


async def open_tab(url: str) -> str:
    """Open (and focus) a tab in the live browser, e.g. a login page for the human to sign in."""
    async with CDP() as c:
        target = (await c.send("Target.createTarget", {"url": url}))["targetId"]
        try:
            await c.send("Target.activateTarget", {"targetId": target})
        except CDPError:
            pass
        return target


def _matches(cookie_domain: str, domain: str) -> bool:
    cd = cookie_domain.lstrip(".").lower()
    d = domain.lstrip(".").lower()
    return cd == d or cd.endswith("." + d)


async def delete_cookies(domains: list[str]) -> int:
    """Sign out of a site by expiring every cookie for its domains."""
    async with CDP() as c:
        cookies = (await c.send("Storage.getCookies")).get("cookies", [])
        doomed = [ck for ck in cookies if any(_matches(ck.get("domain", ""), d) for d in domains)]
        if doomed:
            await c.send("Storage.setCookies", {"cookies": [
                {"name": ck["name"], "value": "", "domain": ck["domain"], "path": ck.get("path", "/"),
                 "secure": ck.get("secure", False), "httpOnly": ck.get("httpOnly", False), "expires": 1}
                for ck in doomed]})
        return len(doomed)


PAGE_STATE_JS = """(() => {
  const pw = [...document.querySelectorAll('input[type=password]')].some(e => e.offsetParent !== null);
  const text = (document.body && document.body.innerText || '').slice(0, 400);
  return JSON.stringify({href: location.href, password: pw, error: location.href.startsWith('chrome-error') ||
    /ERR_[A-Z_]+/.test(text), title: document.title});
})()"""


async def probe(url: str, timeout: float = 20) -> dict[str, Any]:
    """Load `url` in a background tab; once redirects settle, report {url, password, error, title}."""
    async with CDP() as c:
        target = (await c.send("Target.createTarget", {"url": url, "background": True}))["targetId"]
        try:
            last, stable_since = "", 0.0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            await asyncio.sleep(1.0)
            while loop.time() < deadline:
                info = (await c.send("Target.getTargetInfo", {"targetId": target}))["targetInfo"]
                cur = info.get("url", "")
                if cur != last:
                    last, stable_since = cur, loop.time()
                elif loop.time() - stable_since > 2.5 and cur not in ("", "about:blank"):
                    break
                await asyncio.sleep(0.5)
            state: dict[str, Any] = {"url": last, "password": False, "error": False, "title": ""}
            try:
                sid = (await c.send("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
                res = await c.send("Runtime.evaluate", {"expression": PAGE_STATE_JS, "returnByValue": True},
                                   session_id=sid)
                page = json.loads(res.get("result", {}).get("value") or "{}")
                state.update(url=page.get("href") or last, password=bool(page.get("password")),
                             error=bool(page.get("error")), title=page.get("title", ""))
            except Exception:
                pass
            return state
        finally:
            try:
                await c.send("Target.closeTarget", {"targetId": target})
            except CDPError:
                pass
