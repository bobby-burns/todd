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
        self.events: list[dict[str, Any]] = []  # events that arrived while waiting for a reply

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
                if "method" in data:
                    self.events.append(data)

        return await asyncio.wait_for(wait(), timeout)

    async def poll_events(self, seconds: float) -> None:
        """Collect events for up to `seconds` (for pages that only report through events)."""
        assert self._ws is not None
        try:
            while True:
                data = json.loads(await asyncio.wait_for(self._ws.recv(), seconds))
                if "method" in data:
                    self.events.append(data)
        except asyncio.TimeoutError:
            pass

    def take(self, method: str) -> dict[str, Any] | None:
        for i, ev in enumerate(self.events):
            if ev.get("method") == method:
                return self.events.pop(i)
        return None


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


# ------------------------------------------------------------------------------------------ CLI sign-in approval
# One step on a CLI's approval page, decided in the page: stop on password/2FA pages (the human's job), fill the
# one-time code, or find the button that approves (never cancel/deny/switch account). Returns what to do next.
APPROVE_JS = r"""(code, hosts) => {
  const host = location.hostname;
  const vis = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
    && getComputedStyle(el).visibility !== 'hidden';
  const text = ((document.body && document.body.innerText) || '').slice(0, 3000);
  if (!hosts.some((h) => host === h || host.endsWith('.' + h))) return {state: 'offsite', host};
  if ([...document.querySelectorAll('input[type=password]')].some(vis)) return {state: 'password', host};
  const otp = [...document.querySelectorAll('input[autocomplete="one-time-code"], input[name*="otp" i], ' +
    'input[id*="otp" i], input[name*="totp" i], input[name*="2fa" i]')].some(vis);
  if (otp || /confirm access|verify (it's|that it's|its) you|two-factor|security key|use (a |your )?passkey/i.test(text))
    return {state: 'verify', host};
  const raw = (code || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
  const setv = (el, v) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const boxes = [...document.querySelectorAll('input:not([type]), input[type=text], input[type=tel]')]
    .filter(vis).filter((i) => !i.disabled && !i.readOnly);
  if (raw) {
    const singles = boxes.filter((i) => i.maxLength === 1);
    if (singles.length >= raw.length) {
      if (singles.slice(0, raw.length).map((i) => i.value).join('').toUpperCase() !== raw) {
        [...raw].forEach((c, k) => setv(singles[k], c));
        return {state: 'filled'};
      }
    } else if (boxes.length === 1 && boxes[0].value.replace(/[^A-Za-z0-9]/g, '').toUpperCase() !== raw) {
      setv(boxes[0], code);
      return {state: 'filled'};
    }
  }
  const label = (b) => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
  const bad = /cancel|deny|decline|reject|not now|go back|sign out|log out|switch|different|another|remove/i;
  const good = /^(continue|authori[sz]e|allow|approve|confirm|next|submit|yes|grant|connect|accept|activate)\b/i;
  const tiles = [...document.querySelectorAll('[data-identifier]')].filter(vis);  // Google's account chooser
  const target = tiles.length === 1 ? tiles[0] : [...document.querySelectorAll(
      'button, input[type=submit], [role=button]')].filter(vis)
    .find((b) => !b.disabled && b.getAttribute('aria-disabled') !== 'true' && good.test(label(b)) && !bad.test(label(b)));
  if (!target) return {state: 'idle'};
  target.scrollIntoView({block: 'center'});
  const r = target.getBoundingClientRect();
  return {state: 'click', label: label(target).slice(0, 60) || 'account', x: r.left + r.width / 2, y: r.top + r.height / 2};
}"""

DONE_PAGE = ("<!doctype html><title>Signed in</title><body style='font:15px system-ui;padding:40px'>"
             "<b>Todd finished this sign-in.</b> You can close this tab.</body>")

_TEXT_JS = r"""(() => { const out = []; const walk = (n) => { if (n.nodeType === 3) out.push(n.nodeValue);
  if (n.tagName === 'INPUT' || n.tagName === 'TEXTAREA') { if (n.type !== 'password') out.push(' ' + n.value + ' '); }
  (n.shadowRoot ? [n.shadowRoot] : []).concat([...(n.childNodes || [])]).forEach(walk); };
  walk(document.body || document); return out.join(' '); })()"""


async def approve(url: str, *, hosts: list[str], code: str | None = None, callback: str | None = None,
                  page_code_re: str | None = None, done: Any = None, on_state: Any = None,
                  auto_seconds: float = 90, timeout: float = 600, max_clicks: int = 8) -> dict[str, Any]:
    """Open a CLI's approval page in the live browser and approve it with the signed-in session.

    For `auto_seconds` Todd fills the one-time code and clicks approve-type buttons (real mouse events), only on
    `hosts`. It stops acting on a password, 2FA or account-confirmation page, or after `max_clicks`, and reports
    "needs_you" through `on_state` so the human can finish in the same tab. Until `timeout` it keeps watching for:
    `done()` (the CLI finished), a redirect to `callback` (caught before the browser loads it: the browser can't reach
    the CLI's localhost, so the URL is returned to be replayed where the CLI runs), or a code on the page matching
    `page_code_re`. Returns {"state": "approved"|"timeout", "callback"?, "page_code"?, "tab"}."""
    import base64
    import re

    loop = asyncio.get_running_loop()
    start = loop.time()
    clicks, handed_over = 0, False

    def report(state: str, message: str = "") -> None:
        if on_state:
            on_state(state, message)

    async with CDP() as c:
        tab = (await c.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        try:
            await c.send("Target.activateTarget", {"targetId": tab})
        except CDPError:
            pass
        sid = (await c.send("Target.attachToTarget", {"targetId": tab, "flatten": True}))["sessionId"]
        if callback:
            await c.send("Fetch.enable", {"patterns": [{"urlPattern": callback + "*", "requestStage": "Request"}]},
                         session_id=sid)
        await c.send("Page.navigate", {"url": url}, session_id=sid)
        report("approving", "Approving in the browser with your signed-in session")
        while loop.time() - start < timeout:
            await c.poll_events(1.2)
            ev = c.take("Fetch.requestPaused")
            if ev:
                caught = ev["params"]["request"]["url"]
                await c.send("Fetch.fulfillRequest", {
                    "requestId": ev["params"]["requestId"], "responseCode": 200,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
                    "body": base64.b64encode(DONE_PAGE.encode()).decode()}, session_id=sid)
                return {"state": "approved", "callback": caught, "tab": tab}
            if done and done():
                return {"state": "approved", "tab": tab}
            try:
                if page_code_re:
                    res = await c.send("Runtime.evaluate", {"expression": _TEXT_JS, "returnByValue": True},
                                       session_id=sid)
                    m = re.search(page_code_re, str(res.get("result", {}).get("value") or ""))
                    if m:
                        return {"state": "approved", "page_code": m.group(1), "tab": tab}
                if handed_over:
                    continue
                res = await c.send("Runtime.evaluate", {
                    "expression": f"({APPROVE_JS})({json.dumps(code)}, {json.dumps(list(hosts))})",
                    "returnByValue": True}, session_id=sid)
            except CDPError:  # navigating: the page is between documents
                continue
            step = res.get("result", {}).get("value") or {}
            state = step.get("state")
            if state in ("password", "verify") or loop.time() - start > auto_seconds or clicks >= max_clicks:
                handed_over = True
                why = {"password": "is asking for your password", "verify": "wants you to confirm it's you (2FA)"}
                report("needs_you", f"The page {why.get(state, 'needs you')}: finish it in the browser panel. "
                                    "Todd continues as soon as it's approved.")
            elif state == "click":
                x, y = float(step["x"]), float(step["y"])
                for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                    await c.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                              "clickCount": 1}, session_id=sid)
                clicks += 1
                report("approving", f"Clicked “{step.get('label')}”")
        return {"state": "timeout", "tab": tab}


async def close_tab(target: str) -> None:
    try:
        async with CDP() as c:
            await c.send("Target.closeTarget", {"targetId": target})
    except Exception:  # noqa: BLE001
        pass
