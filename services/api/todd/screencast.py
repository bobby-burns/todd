"""Recording the product in the agents' browser: Todd built it, so it knows the pages and the flows, and films them
itself.

A phone-sized tab (414×736 CSS px at 2.6×, so frames are exactly 1080×1920) or a desktop one (1440×810), driven by
simple steps, captured with CDP screencast. Every frame and every step has a timestamp, so a shot can land a moment of
the recording on a spoken word (shorts_plan: `sync.at_s`).

Recording reads pages freely, but acts carefully: taps and typing only on the product's own pages, never on social
sites or payment pages, never on a button that buys, posts, sends, deletes or approves, never into a password field,
and never a form submit. A recording is for showing the product, not for doing things with the human's accounts.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import re
import time
from typing import Any

import httpx
from websockets.asyncio.client import connect

from .agents.browser import cdp_url
from .gates import host_of, is_local, is_payment_host, is_public_site

DEVICES: dict[str, dict[str, Any]] = {
    "phone": {"width": 414, "height": 736, "scale": 1080 / 414, "mobile": True},  # 9:16
    "desktop": {"width": 1440, "height": 810, "scale": 1.0, "mobile": False},  # 16:9
}
PHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1")
MAX_S = 45
MAX_STEPS = 40
RISKY = re.compile(r"\b(buy|pay|purchase|checkout|check out|order|subscribe|upgrade|post|publish|tweet|send|share|"
                   r"delete|remove|approve|confirm|submit|donate|deploy|launch|go live|unsubscribe|cancel plan)\b",
                   re.I)
STEP_KEYS = ("wait", "scroll", "scroll_to", "tap", "type", "goto", "mark")

FIND_JS = r"""(q, kind) => {
  const vis = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  // every name a person might use for an element: its text, label, placeholder, aria-label, title, name
  const names = (el) => [el.innerText, el.value, el.getAttribute('aria-label'), el.getAttribute('placeholder'),
    el.title, el.labels && [...el.labels].map((l) => l.innerText).join(' '), el.name].map(norm).filter(Boolean);
  const want = norm(q);
  const sel = kind === 'field' ? 'input:not([type=hidden]), textarea, select, [contenteditable="true"]'
    : 'button, a, [role=button], [role=tab], [role=link], [role=menuitem], summary, label, input[type=button]';
  const pool = [...document.querySelectorAll(sel)].filter(vis);
  let el = pool.find((e) => names(e).includes(want)) || pool.find((e) => names(e).some((n) => n.includes(want)));
  const byText = () => [...document.querySelectorAll('body *')].filter(vis)
    .filter((e) => e.childElementCount === 0).find((e) => norm(e.innerText) === want)
    || [...document.querySelectorAll('body *')].filter(vis).filter((e) => e.childElementCount === 0)
    .find((e) => norm(e.innerText).includes(want));
  if (!el && kind === 'text') el = byText();
  if (!el && kind === 'field') {  // a heading or label above the field, not linked to it: the next field after it
    const t = byText();
    if (t) el = pool.find((f) => t.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING);
  }
  if (!el) return null;
  document.querySelectorAll('[data-todd-rec]').forEach((e) => e.removeAttribute('data-todd-rec'));
  el.setAttribute('data-todd-rec', '1');
  return {label: (names(el)[0] || '').slice(0, 80), tag: el.tagName.toLowerCase(),
          type: (el.type || '').toLowerCase()};
}"""
# Smooth-scroll whatever actually scrolls: the page, or (when the page itself doesn't) its biggest scrolling box.
# Returns how far it can still go, so the caller knows the scroll happened.
SCROLL_JS = r"""(px) => {
  const root = document.scrollingElement || document.documentElement;
  let el = root;
  if (root.scrollHeight <= innerHeight + 4) {
    el = [...document.querySelectorAll('*')].filter((e) => { const st = getComputedStyle(e);
      return /(auto|scroll)/.test(st.overflowY) && e.scrollHeight > e.clientHeight + 4; })
      .sort((a, b) => b.clientHeight * b.clientWidth - a.clientHeight * a.clientWidth)[0] || root;
  }
  const top = px === 'top' ? -el.scrollTop : px === 'bottom' ? el.scrollHeight : px;
  el.scrollBy({top, behavior: 'smooth'});
  return {from: el.scrollTop, room: el.scrollHeight - el.clientHeight};
}"""
POS_JS = r"""() => { const root = document.scrollingElement || document.documentElement;
  const boxes = [root, ...document.querySelectorAll('*')].filter((e) => e.scrollTop > 0);
  return Math.max(0, ...boxes.map((e) => e.scrollTop)); }"""
RECT_JS = r"""() => { const el = document.querySelector('[data-todd-rec]'); if (!el) return null;
  const r = el.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2,
  inView: r.top >= 0 && r.bottom <= innerHeight}; }"""


class RecordError(Exception):
    pass


def check_steps(steps: Any) -> list[dict[str, Any]]:
    """Validate the steps before anything opens: [{"wait": 1}, {"scroll": 600}, {"tap": "Games"}, …]."""
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise RecordError(f"steps is a list of up to {MAX_STEPS} steps")
    out = []
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict) or len([k for k in st if k in STEP_KEYS]) != 1:
            raise RecordError(f"step {i}: one of {', '.join(STEP_KEYS)} (e.g. {{\"tap\": \"Games\"}})")
        key = next(k for k in st if k in STEP_KEYS)
        val = st[key]
        if key == "wait" and not (isinstance(val, (int, float)) and 0 <= val <= 10):
            raise RecordError(f"step {i}: wait is 0–10 seconds")
        if key == "scroll" and not (val in ("top", "bottom") or (isinstance(val, (int, float)) and abs(val) <= 5000)):
            raise RecordError(f"step {i}: scroll is a number of pixels (negative = up), \"top\" or \"bottom\"")
        if key in ("scroll_to", "tap", "mark") and not (isinstance(val, str) and 0 < len(val) <= 120):
            raise RecordError(f"step {i}: {key} is the visible text to look for")
        if key == "type":
            if not isinstance(val, str) or not 0 < len(val) <= 200 or not isinstance(st.get("into"), str):
                raise RecordError(f"step {i}: {{\"type\": text, \"into\": the field's label or placeholder}}")
        if key == "goto" and not (isinstance(val, str) and val.startswith(("http://", "https://"))):
            raise RecordError(f"step {i}: goto is an http(s) address")
        out.append(st)
    return out


def may_act(url: str, label: str = "", kind: str = "") -> str | None:
    """Why a tap or typing must not happen in a recording, or None."""
    host = host_of(url)
    if not is_local(host) and (is_public_site(host) or is_payment_host(host)):
        return f"{host} acts for the human (posts, payments): a recording only reads it"
    if RISKY.search(label or ""):
        return f"\"{label}\" looks like it buys, posts, sends, deletes or approves something: recordings don't press it"
    if kind == "submit":
        return "recordings don't submit forms"
    return None


class _Tab:
    """One recording tab with its own CDP connection; screencast frames are acknowledged as they arrive."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self.sid: str | None = None
        self.frames: list[tuple[float, bytes]] = []
        self.idle = asyncio.Event()

    async def open(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                info = (await c.get(f"{cdp_url()}/json/version")).json()
        except Exception as e:  # noqa: BLE001
            raise RecordError(f"the browser isn't reachable at {cdp_url()}: {e}") from e
        self._ws = await connect(info["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024, open_timeout=10)
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def _read(self) -> None:
        async for raw in self._ws:
            msg = json.loads(raw)
            if "id" in msg:
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RecordError(msg["error"].get("message", "CDP error")))
                    else:
                        fut.set_result(msg.get("result", {}))
            elif msg.get("method") == "Page.screencastFrame" and msg.get("sessionId") == self.sid:
                p = msg["params"]
                self.frames.append((time.time(), base64.b64decode(p["data"])))
                asyncio.create_task(self.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, wait=False))
            elif msg.get("method") == "Page.lifecycleEvent" and msg["params"].get("name") == "networkIdle":
                self.idle.set()

    async def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 20,
                   page: bool = True, wait: bool = True) -> dict[str, Any]:
        msg_id = next(self._ids)
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if page and self.sid:
            msg["sessionId"] = self.sid
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps(msg))
        if not wait:
            return {}
        return await asyncio.wait_for(fut, timeout)

    async def js(self, fn: str, *args: Any) -> Any:
        expr = f"({fn})(...{json.dumps(list(args))})"
        r = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return (r.get("result") or {}).get("value")


async def _load(tab: _Tab, url: str) -> None:
    tab.idle.clear()
    await tab.send("Page.navigate", {"url": url})
    try:
        await asyncio.wait_for(tab.idle.wait(), 12)
    except asyncio.TimeoutError:
        pass  # pages that never go quiet (polling, websockets) still get recorded
    await asyncio.sleep(0.4)


async def _current_url(tab: _Tab) -> str:
    return str(await tab.js("() => location.href") or "")


async def _find(tab: _Tab, text: str, kind: str) -> dict[str, Any]:
    found = await tab.js(FIND_JS, text, kind)
    if not found:
        raise RecordError(f"nothing on the page says {text!r}")
    rect = await tab.js(RECT_JS)
    if rect and not rect["inView"]:  # bring it into view on camera, then measure again
        await tab.js("() => document.querySelector('[data-todd-rec]').scrollIntoView({block: 'center', "
                     "behavior: 'smooth'})")
        await asyncio.sleep(0.8)
        rect = await tab.js(RECT_JS)
    return {**found, **(rect or {})}


async def _tap(tab: _Tab, x: float, y: float, mobile: bool) -> None:
    if mobile:
        pt = [{"x": x, "y": y}]
        await tab.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pt})
        await asyncio.sleep(0.08)
        await tab.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    else:
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await tab.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                        "clickCount": 1})


async def _step(tab: _Tab, st: dict[str, Any], dev: dict[str, Any]) -> str:
    key = next(k for k in st if k in STEP_KEYS)
    val = st[key]
    if key == "wait":
        await asyncio.sleep(float(val))
        return f"wait {val}s"
    if key == "mark":
        return f"mark {val}"
    if key == "goto":
        await _load(tab, val)
        return f"goto {val}"
    if key == "scroll":
        # A synthesized touch gesture reports success without moving some pages, so scroll by script (eased, like a
        # thumb) and wait for it to land.
        before = await tab.js(POS_JS)
        info = await tab.js(SCROLL_JS, val) or {}
        dist = abs(float(val)) if isinstance(val, (int, float)) else float(info.get("room") or 800)
        await asyncio.sleep(min(1.8, 0.5 + dist / 900))
        if abs((await tab.js(POS_JS) or 0) - (before or 0)) < 2 and info.get("room", 0) > 2:
            raise RecordError(f"the page didn't scroll ({val})")
        return f"scroll {val}"
    if key == "scroll_to":
        await _find(tab, val, "text")
        return f"scroll to {val!r}"
    if key == "tap":
        el = await _find(tab, val, "control")
        why = may_act(await _current_url(tab), el.get("label", ""), el.get("type", ""))
        if why:
            raise RecordError(why)
        await _tap(tab, el["x"], el["y"], dev["mobile"])
        await asyncio.sleep(0.6)
        return f"tap {val!r}"
    if key == "type":
        el = await _find(tab, st["into"], "field")
        if el.get("type") == "password":
            raise RecordError("recordings never type into password fields")
        why = may_act(await _current_url(tab))
        if why:
            raise RecordError(why)
        await tab.js("() => document.querySelector('[data-todd-rec]').focus()")
        for ch in val:  # a person's typing pace
            await tab.send("Input.insertText", {"text": ch})
            await asyncio.sleep(0.06)
        return f"type into {st['into']!r}"
    raise RecordError(f"unknown step {key}")


async def record(url: str, steps: list[dict[str, Any]], device: str = "phone",
                 max_s: float = MAX_S) -> dict[str, Any]:
    """Record `url` while running `steps`. Returns {frames: [(t, jpeg)], marks: [{t, step}], duration_s, width,
    height}; t is seconds from the start of the recording. The caller holds the browser lock."""
    if device not in DEVICES:
        raise RecordError(f"device is one of {', '.join(DEVICES)}")
    if not url.startswith(("http://", "https://")):
        raise RecordError("url is an http(s) address")
    steps = check_steps(steps)
    dev = DEVICES[device]
    tab = _Tab()
    await tab.open()
    target = None
    try:
        target = (await tab.send("Target.createTarget", {"url": "about:blank", "newWindow": True},
                                 page=False))["targetId"]
        tab.sid = (await tab.send("Target.attachToTarget", {"targetId": target, "flatten": True},
                                  page=False))["sessionId"]
        for method, params in (
                ("Page.enable", {}), ("Runtime.enable", {}), ("Page.setLifecycleEventsEnabled", {"enabled": True}),
                ("Emulation.setDeviceMetricsOverride", {"width": dev["width"], "height": dev["height"],
                                                        "deviceScaleFactor": dev["scale"], "mobile": dev["mobile"],
                                                        "screenWidth": dev["width"], "screenHeight": dev["height"]}),
                ("Emulation.setScrollbarsHidden", {"hidden": True})):
            await tab.send(method, params)
        if dev["mobile"]:
            await tab.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
            await tab.send("Emulation.setUserAgentOverride", {"userAgent": PHONE_UA, "platform": "iPhone"})
        await _load(tab, url)
        w, h = round(dev["width"] * dev["scale"]), round(dev["height"] * dev["scale"])
        start = time.time()
        await tab.send("Page.startScreencast", {"format": "jpeg", "quality": 82, "maxWidth": w, "maxHeight": h,
                                                "everyNthFrame": 1})
        marks: list[dict[str, Any]] = [{"t": 0.0, "step": f"open {url}"}]
        await asyncio.sleep(0.6)
        for st in steps:
            if time.time() - start > max_s:
                marks.append({"t": round(time.time() - start, 3), "step": "stopped: max length reached"})
                break
            t = time.time() - start
            what = await _step(tab, st, dev)
            marks.append({"t": round(t, 3), "step": what})
        await asyncio.sleep(0.8)
        end = time.time()
        await tab.send("Page.stopScreencast", {})
        await asyncio.sleep(0.2)
        frames = [(round(ts - start, 4), data) for ts, data in tab.frames if ts <= end]
        if not frames:
            raise RecordError("the browser sent no frames (is the page blank?)")
        return {"frames": frames, "marks": marks, "duration_s": round(end - start, 3), "width": w, "height": h}
    finally:
        if target:
            try:
                await tab.send("Target.closeTarget", {"targetId": target}, page=False, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        await tab.close()
