"""Recording the product in the agents' browser: Todd built it, so it knows the pages and the flows, and films them
itself.

A phone-sized tab (414×736 CSS px at 2.6×, so frames are exactly 1080×1920) or a desktop one (1440×810), driven by
simple steps, captured with CDP screencast. Every frame and every step has a timestamp, so a shot can land a moment of
the recording on a spoken word (shorts_plan: `sync.at_s`).

A recording runs in a fresh browser context by default: nobody is signed in, so it acts on the product like any
first-time visitor would (answers a quiz, searches, fills a demo form and submits it). It still never pays, buys,
subscribes, deletes, deploys or publishes, never types into a password field, and never acts on social or payment
sites. `signed_in=True` films pages behind the human's sign-in instead (the agents' own browser context), and then it
only reads and makes safe taps: nothing that posts, sends, approves, and no form submits, since there it would act as
the human.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
LOAD_WAIT_S = 8  # a page that never goes quiet (polling, websockets) is filmed anyway after this
# Chrome's screencast sends 60 frames a second, but at CSS-pixel size (414×736 for the phone, not 1080×1920), so
# recordings are filmed with full-resolution screenshots instead (about 15 distinct frames a second: plenty for taps,
# typing and small animations) and use the screencast only while scrolling, where the motion hides the softness.
RISKY = re.compile(r"\b(buy|pay|purchase|checkout|check out|order|subscribe|upgrade|post|publish|tweet|send|share|"
                   r"delete|remove|approve|confirm|submit|donate|deploy|launch|go live|unsubscribe|cancel plan)\b",
                   re.I)
# A visitor with no account can still spend money or change something for real: short command labels that do are
# never pressed, signed in or not. (Only short labels: a quiz answer that mentions "publishing" is just text.)
NEVER = re.compile(r"\b(buy|pay|payment|purchase|checkout|check out|place order|donate|subscribe|upgrade|delete|"
                   r"unsubscribe|cancel (plan|subscription)|deploy|go live|publish)\b", re.I)
COMMAND_WORDS = 6
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
  // the smallest visible element showing the text (a quiz answer is often a <div> with a <span> inside)
  const byText = () => {
    const all = [...document.querySelectorAll('body *')].filter(vis);
    const exact = all.filter((e) => norm(e.innerText) === want);
    const area = (e) => { const r = e.getBoundingClientRect(); return r.width * r.height; };
    return (exact.length ? exact : all.filter((e) => norm(e.innerText).includes(want)))
      .sort((a, b) => area(a) - area(b))[0];
  };
  // text works for taps too: a tap lands on the screen, so clicking a clickable <div> by its words just works
  if (!el && (kind === 'text' || kind === 'control')) el = byText();
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
  const room = el.scrollHeight - el.clientHeight;
  const ahead = top < 0 ? el.scrollTop : room - el.scrollTop;  // how far it can go this way
  if (ahead > 2) el.scrollBy({top, behavior: 'smooth'});
  return {from: el.scrollTop, room, ahead};
}"""
POS_JS = r"""() => { const root = document.scrollingElement || document.documentElement;
  const boxes = [root, ...document.querySelectorAll('*')].filter((e) => e.scrollTop > 0);
  return Math.max(0, ...boxes.map((e) => e.scrollTop)); }"""
# What a page offers, top to bottom, with where each thing is (in screens from the top), so an agent can write steps
# that work the first time instead of guessing at button texts.
OUTLINE_JS = r"""() => {
  const vis = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const H = innerHeight, root = document.scrollingElement || document.documentElement;
  const boxes = [root, ...document.querySelectorAll('*')].filter((e) => e.scrollTop > 0);
  const top = Math.max(0, ...boxes.map((e) => e.scrollTop));
  const big = [root, ...document.querySelectorAll('*')].reduce((m, e) => Math.max(m, e.scrollHeight), 0);
  const seen = new Set(), items = [];
  const add = (kind, el, text) => { text = norm(text).slice(0, 90); if (!text || seen.has(kind + text)) return;
    seen.add(kind + text);
    items.push({kind, text, screen: Math.round((el.getBoundingClientRect().top + top) / H * 10) / 10}); };
  document.querySelectorAll('h1, h2, h3').forEach((e) => vis(e) && add('heading', e, e.innerText));
  document.querySelectorAll('button, a, [role=button], [role=tab], summary, input[type=button], input[type=submit], ' +
    'label, [onclick], [tabindex="0"]').forEach((e) => vis(e) && add(e.tagName === 'A' ? 'link' : 'tap', e,
      e.innerText || e.value || e.getAttribute('aria-label') || e.title));
  document.querySelectorAll('input:not([type=hidden]):not([type=button]):not([type=submit]), textarea, select')
    .forEach((e) => vis(e) && add('field', e, (e.labels && [...e.labels].map((l) => l.innerText).join(' ')) ||
      e.placeholder || e.getAttribute('aria-label') || e.name));
  items.sort((a, b) => a.screen - b.screen);
  return {url: location.href, title: document.title, at: Math.round(top / H * 10) / 10,
          screens: Math.round(Math.max(big, H) / H * 10) / 10, items: items.slice(0, 70)};
}"""
RECT_JS = r"""() => { const el = document.querySelector('[data-todd-rec]'); if (!el) return null;
  const r = el.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2,
  inView: r.top >= 0 && r.bottom <= innerHeight}; }"""


class RecordError(Exception):
    """A recording couldn't go on. `shot` (JPEG, base64) is what the screen showed when it stopped."""

    def __init__(self, message: str, shot: str | None = None) -> None:
        super().__init__(message)
        self.shot = shot


def outline_text(o: dict[str, Any] | None) -> str:
    """The page outline as lines an agent can write steps from."""
    if not o:
        return "(the page couldn't be read)"
    lines = [f"{o.get('url')} \"{(o.get('title') or '')[:80]}\": {o.get('screens')} screens tall, showing from "
             f"screen {o.get('at')}. Things on it (screen: kind: text):"]
    lines += [f"  {it['screen']:g}: {it['kind']}: {it['text']}" for it in o.get("items") or []]
    if len(lines) == 1:
        lines.append("  nothing to tap or read yet (still loading, or drawn on a canvas)")
    return "\n".join(lines)[:4000]


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


def may_act(url: str, label: str = "", kind: str = "", signed_in: bool = False) -> str | None:
    """Why a tap or typing must not happen in a recording, or None. Signed out (the default), a recording acts like
    any visitor; signed in, it would act as the human, so it only makes safe taps."""
    host = host_of(url)
    if not is_local(host) and (is_public_site(host) or is_payment_host(host)):
        return f"{host} is a social or payment site: a recording only reads it"
    label = " ".join((label or "").split())
    if len(label.split()) <= COMMAND_WORDS and NEVER.search(label):
        return f"\"{label}\" looks like it spends money, deletes or publishes something: recordings never press it"
    if signed_in:
        if RISKY.search(label):
            return (f"\"{label}\" looks like it posts, sends or approves something as the human: a signed-in "
                    "recording doesn't press it (record signed out to act like a visitor)")
        if kind == "submit":
            return "a signed-in recording doesn't submit forms (record signed out to act like a visitor)"
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
        self.moving = 0  # a scroll is on camera
        self.held = 0  # a tap is being pressed: no screenshot now
        self.inflight = 0  # a screenshot asked for and not back yet
        self.motion_gen = 0
        self.motion: list[tuple[float, float]] = []  # when the scrolls were
        self.point: tuple[float, float] | None = None  # where the last tap or typing landed, in CSS px

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
            elif msg.get("method") == "Page.lifecycleEvent" and \
                    msg["params"].get("name") in ("networkIdle", "networkAlmostIdle"):
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
        await asyncio.wait_for(tab.idle.wait(), LOAD_WAIT_S)
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
        async with _moving(tab):
            await tab.js("() => document.querySelector('[data-todd-rec]').scrollIntoView({block: 'center', "
                         "behavior: 'smooth'})")
            await asyncio.sleep(0.8)
        rect = await tab.js(RECT_JS)
    return {**found, **(rect or {})}


async def _tap(tab: _Tab, x: float, y: float, mobile: bool) -> None:
    # Input waits behind a screenshot in progress, and a touch held too long stops being a tap: let it land first,
    # and take none during the press.
    tab.held += 1
    try:
        for _ in range(50):
            if not tab.inflight:
                break
            await asyncio.sleep(0.01)
        await _press(tab, x, y, mobile)
    finally:
        tab.held -= 1


async def _press(tab: _Tab, x: float, y: float, mobile: bool) -> None:
    if mobile:
        pt = [{"x": x, "y": y}]
        await tab.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pt})
        await asyncio.sleep(0.08)
        await tab.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    else:
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await tab.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                        "clickCount": 1})


async def _step(tab: _Tab, st: dict[str, Any], dev: dict[str, Any], signed_in: bool = False) -> str:
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
        async with _moving(tab):
            info = await tab.js(SCROLL_JS, val) or {}
            if info.get("ahead", 1) > 2:
                ahead = float(info.get("ahead") or 800)
                dist = min(abs(float(val)), ahead) if isinstance(val, (int, float)) else ahead
                await asyncio.sleep(min(1.8, 0.5 + dist / 900))
        if info.get("ahead", 1) <= 2:  # already at the top or bottom: the page just stays put
            up = val == "top" or (isinstance(val, (int, float)) and val < 0)
            await asyncio.sleep(0.4)
            return f"scroll {val} (already at the {'top' if up else 'bottom'})"
        if abs((await tab.js(POS_JS) or 0) - (before or 0)) < 2:
            raise RecordError(f"the page didn't scroll ({val})")
        return f"scroll {val}"
    if key == "scroll_to":
        await _find(tab, val, "text")
        return f"scroll to {val!r}"
    if key == "tap":
        el = await _find(tab, val, "control")
        why = may_act(await _current_url(tab), el.get("label", ""), el.get("type", ""), signed_in)
        if why:
            raise RecordError(why)
        tab.point = (el["x"], el["y"])
        await _tap(tab, el["x"], el["y"], dev["mobile"])
        await asyncio.sleep(0.6)
        return f"tap {val!r}"
    if key == "type":
        el = await _find(tab, st["into"], "field")
        tab.point = (el["x"], el["y"]) if el.get("x") is not None else None
        if el.get("type") == "password":
            raise RecordError("recordings never type into password fields")
        why = may_act(await _current_url(tab), signed_in=signed_in)
        if why:
            raise RecordError(why)
        await tab.js("() => document.querySelector('[data-todd-rec]').focus()")
        for ch in val:  # a person's typing pace
            await tab.send("Input.insertText", {"text": ch})
            await asyncio.sleep(0.06)
        return f"type into {st['into']!r}"
    raise RecordError(f"unknown step {key}")


@asynccontextmanager
async def _moving(tab: "_Tab") -> AsyncIterator[None]:
    """A scroll on camera: the screencast's smooth frames are used for it, the sharp screenshots around it."""
    tab.moving += 1
    tab.motion_gen += 1
    t0 = time.time()
    try:
        yield
    finally:
        tab.moving -= 1
        tab.motion.append((t0, time.time() + 0.1))


async def _stills(tab: "_Tab", shots: list[tuple[float, str]]) -> None:
    """While recording: full-resolution screenshots whenever nothing is scrolling (about 15 distinct frames a
    second). One at a time: with two in flight, Chrome maps later taps 2.6× off (the emulated pixel density). A shot
    that overlapped the start of a scroll is dropped."""
    async def worker() -> None:
        while True:
            if tab.moving or tab.held:
                await asyncio.sleep(0.02)
                continue
            gen, t = tab.motion_gen, time.time()
            tab.inflight += 1
            try:
                r = await tab.send("Page.captureScreenshot", {"format": "jpeg", "quality": 85,
                                                              "optimizeForSpeed": True}, timeout=5)
            except (RecordError, asyncio.TimeoutError):
                await asyncio.sleep(0.05)
                continue
            finally:
                tab.inflight -= 1
            if not tab.moving and tab.motion_gen == gen and r.get("data"):
                shots.append((t, r["data"]))
    await worker()


def _merge(cast: list[tuple[float, bytes]], shots: list[tuple[float, str]],
           motion: list[tuple[float, float]]) -> list[tuple[float, bytes]]:
    """Sharp screenshots for the still parts, screencast frames for the scrolls; repeats of the same image dropped."""
    if not shots:
        return list(cast)
    moving = lambda t: any(a <= t <= b for a, b in motion)  # noqa: E731
    out: list[tuple[float, Any]] = [(t, d) for t, d in cast if moving(t)]
    last = None
    for t, d in sorted(shots):
        if not moving(t) and d != last:
            out.append((t, base64.b64decode(d)))
            last = d
    return sorted(out, key=lambda x: x[0])


class _Session:
    """A phone- or desktop-sized tab in the agents' browser, in a fresh signed-out context unless `signed_in`."""

    def __init__(self, device: str, signed_in: bool) -> None:
        if device not in DEVICES:
            raise RecordError(f"device is one of {', '.join(DEVICES)}")
        self.dev, self.signed_in = DEVICES[device], signed_in
        self.tab = _Tab()
        self.target: str | None = None
        self.context: str | None = None

    async def __aenter__(self) -> "_Session":
        tab, dev = self.tab, self.dev
        await tab.open()
        try:
            where: dict[str, Any] = {"url": "about:blank", "newWindow": True}
            if not self.signed_in:  # a fresh context: no cookies or storage from the human's sessions
                self.context = (await tab.send("Target.createBrowserContext", {"disposeOnDetach": True},
                                               page=False))["browserContextId"]
                where["browserContextId"] = self.context
            self.target = (await tab.send("Target.createTarget", where, page=False))["targetId"]
            tab.sid = (await tab.send("Target.attachToTarget", {"targetId": self.target, "flatten": True},
                                      page=False))["sessionId"]
            metrics = {"width": dev["width"], "height": dev["height"], "deviceScaleFactor": dev["scale"],
                       "mobile": dev["mobile"], "screenWidth": dev["width"], "screenHeight": dev["height"]}
            for method, params in (
                    ("Page.enable", {}), ("Runtime.enable", {}), ("Page.setLifecycleEventsEnabled", {"enabled": True}),
                    ("Emulation.setDeviceMetricsOverride", metrics),
                    ("Emulation.setScrollbarsHidden", {"hidden": True})):
                await tab.send(method, params)
            if dev["mobile"]:
                await tab.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
                await tab.send("Emulation.setUserAgentOverride", {"userAgent": PHONE_UA, "platform": "iPhone"})
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        tab = self.tab
        if self.target:
            try:
                await tab.send("Target.closeTarget", {"targetId": self.target}, page=False, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self.context:
            try:
                await tab.send("Target.disposeBrowserContext", {"browserContextId": self.context}, page=False,
                               timeout=5)
            except Exception:  # noqa: BLE001
                pass
        await tab.close()

    async def go(self, url: str, start_at: str | None = None) -> None:
        await _load(self.tab, url)
        if start_at:  # position the page off camera
            if not await self.tab.js(FIND_JS, start_at, "text"):
                raise await self.stuck(f"start_at: nothing on the page says {start_at!r}")
            # 'instant': a page with `scroll-behavior: smooth` would otherwise animate, and the next scroll cut it short
            await self.tab.js("() => document.querySelector('[data-todd-rec]').scrollIntoView({block: 'start', "
                              "behavior: 'instant'})")
            await self.tab.js("() => window.scrollBy({top: -Math.round(innerHeight * 0.12), behavior: 'instant'})")
            await asyncio.sleep(0.4)

    async def outline(self) -> dict[str, Any] | None:
        try:
            return await self.tab.js(OUTLINE_JS)
        except Exception:  # noqa: BLE001
            return None

    async def screenshot(self) -> str | None:
        """The screen as a JPEG (base64), for the agent to look at."""
        try:
            return (await self.tab.send("Page.captureScreenshot", {"format": "jpeg", "quality": 70},
                                        timeout=10)).get("data")
        except Exception:  # noqa: BLE001
            return None

    async def stuck(self, why: str) -> RecordError:
        """An error that says what went wrong and what the page shows instead, with a screenshot."""
        page = outline_text(await self.outline())
        return RecordError(f"{why}\nNothing was saved. What the page shows now:\n{page}", await self.screenshot())


async def look(url: str, device: str = "phone", start_at: str | None = None,
               signed_in: bool = False) -> dict[str, Any]:
    """Open `url` the way a recording would, without filming: {outline, shot (JPEG base64)}. The caller holds the
    browser lock."""
    if not url.startswith(("http://", "https://")):
        raise RecordError("url is an http(s) address")
    async with _Session(device, signed_in) as ses:
        await ses.go(url, start_at)
        return {"outline": await ses.outline(), "shot": await ses.screenshot()}


async def record(url: str, steps: list[dict[str, Any]], device: str = "phone", max_s: float = MAX_S,
                 start_at: str | None = None, signed_in: bool = False) -> dict[str, Any]:
    """Record `url` while running `steps`. Returns {frames: [(t, jpeg)], marks: [{t, step}], duration_s, width,
    height}; t is seconds from the start of the recording. `start_at`: visible text to bring to the top of the screen
    before filming starts (off camera). `signed_in`: film in the agents' browser context, with the human's sign-ins,
    instead of a fresh one. A step that can't run stops it with what the page shows instead. The caller holds the
    browser lock."""
    if not url.startswith(("http://", "https://")):
        raise RecordError("url is an http(s) address")
    steps = check_steps(steps)
    async with _Session(device, signed_in) as ses:
        tab, dev = ses.tab, ses.dev
        await ses.go(url, start_at)
        w, h = round(dev["width"] * dev["scale"]), round(dev["height"] * dev["scale"])
        start = time.time()
        await tab.send("Page.startScreencast", {"format": "jpeg", "quality": 82, "maxWidth": w, "maxHeight": h,
                                                "everyNthFrame": 1})
        shots: list[tuple[float, str]] = []
        stills = asyncio.create_task(_stills(tab, shots))
        marks: list[dict[str, Any]] = [{"t": 0.0, "step": f"open {url}"}]
        try:
            await asyncio.sleep(0.6)
            for n, st in enumerate(steps, 1):
                if time.time() - start > max_s:
                    marks.append({"t": round(time.time() - start, 3), "step": "stopped: max length reached"})
                    break
                t = time.time() - start
                try:
                    what = await _step(tab, st, dev, signed_in)
                except RecordError as e:
                    raise await ses.stuck(f"step {n} {json.dumps(st)} failed: {e}") from e
                mark: dict[str, Any] = {"t": round(t, 3), "step": what}
                if tab.point:  # where it happened, as shares of the frame: a punch-in can centre on it (focus x, y)
                    mark |= {"x": round(min(max(tab.point[0] / dev["width"], 0), 1), 3),
                             "y": round(min(max(tab.point[1] / dev["height"], 0), 1), 3)}
                    tab.point = None
                marks.append(mark)
            await asyncio.sleep(0.8)
        finally:
            stills.cancel()
            await asyncio.gather(stills, return_exceptions=True)
        end = time.time()
        await tab.send("Page.stopScreencast", {})
        await asyncio.sleep(0.2)
        length = round(end - start, 3)  # frame times are clamped into [0, length], so rounding never pushes one past it
        frames = [(min(max(round(ts - start, 4), 0.0), length), data)
                  for ts, data in _merge(tab.frames, shots, tab.motion) if ts <= end]
        if not frames:
            raise RecordError("the browser sent no frames (is the page blank?)")
        return {"frames": frames, "marks": marks, "duration_s": length, "width": w, "height": h}
