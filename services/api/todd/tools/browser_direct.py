"""`browser` toolset for the Claude Code engine: the agent drives the shared Chromium itself, step by step.

Under the API engine, `browse(task)` hands a whole task to a browser-use agent with its own LLM. Under the Claude
Code engine every model call must go through the human's Claude plan, so the agent uses browser-use's proven
primitives directly (indexed page elements, click/type/scroll/navigate over CDP) and is its own browser agent.

Same guarantees as `browse`:
* API/MCP first: `browser_start` requires `why_not_api`, shown in the agent's window.
* One shared browser: `browser_start` takes a run-wide lock; `browser_done` (or the agent ending) releases it.
* Payments: card details are only available after the human approves the amount, only as placeholders
  (<secret>card_number</secret> …) that browser-use fills in on the approved domains; screenshots are off while
  card details may be on screen, and every tool result is scrubbed of vault values.
* Each step is shown in the dashboard with a screenshot; the model also gets the screenshot.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from typing import Any

from ..agents.browser import _card_secrets, cdp_url
from ..config import config
from ..policy import authorize_spend, settle
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from .browser_tools import clean_domain

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")

MAX_DOM_CHARS = 14000


@dataclass
class _Handle:
    browser: Any
    tools: Any
    sensitive: dict | None
    ledger: Any
    reason: str
    steps: int = 0


def _handles(ctx) -> dict[str, _Handle]:
    if not hasattr(ctx, "_browser_handles"):
        ctx._browser_handles = {}
    return ctx._browser_handles


def _start_lock(ctx, agent_id: str) -> asyncio.Lock:
    """Serializes one agent's own browser_start calls (parallel calls in one turn must not queue on each other)."""
    if not hasattr(ctx, "_browser_start_locks"):
        ctx._browser_start_locks = {}
    return ctx._browser_start_locks.setdefault(agent_id, asyncio.Lock())


_VALUE_ATTR = re.compile(r"(value=)(.*?)(?=\s[\w-]+=|\s?/?>)")


def _redactor(sensitive: dict | None):
    """While card details may be on the page: hide every form value in the page state and scrub card values in any
    format (e.g. "4242 4242 …" after a form reformats it), so they never reach the model, the timeline or logs."""
    if not sensitive:
        return lambda text: text
    values = {v for d in sensitive.values() if isinstance(d, dict) for v in d.values() if v}
    pats = []
    for v in sorted(values, key=len, reverse=True):
        digits = re.sub(r"\D", "", v)
        if len(digits) >= 3 and len(digits) >= len(v) - 3:
            pats.append(re.compile(r"[\s./-]*".join(map(re.escape, digits))))
        if len(v) >= 3:
            pats.append(re.compile(re.escape(v), re.I))

    def redact(text: str) -> str:
        text = _VALUE_ATTR.sub(r"\1[hidden]", text)
        for p in pats:
            text = p.sub("[card]", text)
        return text

    return redact


def _get() -> tuple[Any, str, _Handle]:
    ctx, agent_id = get_ctx(), get_agent_id()
    h = _handles(ctx).get(agent_id)
    if h is None:
        raise ToolError("You don't have the browser. Call browser_start(why_not_api=...) first.")
    return ctx, agent_id, h


async def _state(ctx, agent_id: str, h: _Handle, note: str) -> str:
    card_mode = h.sensitive is not None
    s = await h.browser.get_browser_state_summary(include_screenshot=not card_mode)
    h.steps += 1
    data: dict[str, Any] = {"step": h.steps, "url": s.url, "title": s.title}
    if s.screenshot and not card_mode:
        shots = config.data_dir / "screens" / ctx.run_id
        shots.mkdir(parents=True, exist_ok=True)
        name = f"{agent_id}-{h.steps:04d}.png"
        try:
            (shots / name).write_bytes(base64.b64decode(s.screenshot))
            data["screenshot"] = f"/api/screens/{ctx.run_id}/{name}"
            ctx.push_image(agent_id, s.screenshot)
        except Exception:
            pass
    ctx.emit(agent_id, "browser_step", note, data)
    dom = _redactor(h.sensitive)(s.dom_state.llm_representation() if s.dom_state else "")
    if len(dom) > MAX_DOM_CHARS:
        dom = dom[:MAX_DOM_CHARS] + "\n…[page truncated: scroll or use browser_search to see more]"
    tabs = ", ".join(f"{t.target_id[-4:]}: {t.title or t.url}"[:80] for t in (s.tabs or []))
    return (f"URL: {s.url}\nTitle: {s.title}\nTabs: {tabs}\n"
            f"Interactive elements are shown as [index]<tag …/>; use the index with browser_click/browser_type.\n\n{dom}")


async def _act(action: str, params: dict[str, Any], note: str) -> str:
    ctx, agent_id, h = _get()
    r = await h.tools.registry.execute_action(action, params, browser_session=h.browser, sensitive_data=h.sensitive)
    msg = (getattr(r, "extracted_content", None) or "").strip()
    err = getattr(r, "error", None)
    if err:
        msg = f"Error: {err}"
    msg = _redactor(h.sensitive)(msg)
    state = await _state(ctx, agent_id, h, note)
    return f"{msg}\n\n{state}" if msg else state


@todd_tool(toolset="browser")
async def browser_start(why_not_api: str, start_url: str | None = None, payment_amount_usd: float | None = None,
                        payment_merchant: str | None = None, payment_domains: list[str] | None = None) -> str:
    """LAST RESORT. Take the shared browser (a real Chromium where the human is signed in to their accounts) for
    consoles without APIs, sign-in-gated pages, forms or card checkout. Call find_integrations first: prefer
    toolsets, MCP servers, api_request and CLIs. Returns the page state; then use the other browser_* tools and
    call browser_done when finished so other agents can use it. Card payment: pass payment_amount_usd,
    payment_merchant and the exact payment_domains; the human must approve, then type card fields as
    <secret>card_number</secret>, <secret>card_exp</secret>, <secret>card_cvc</secret>, <secret>card_name</secret>,
    <secret>card_zip</secret> (real values are filled in on those domains only).

    Args:
        why_not_api: why no API, MCP server or CLI can do this (shown to the human)
        start_url: page to open first
        payment_amount_usd: maximum card payment, if this task must pay by card
        payment_merchant: who is being paid
        payment_domains: exact domains where card details may be entered, e.g. ["checkout.stripe.com"]
    """
    ctx, agent_id = get_ctx(), get_agent_id()
    if not why_not_api or len(why_not_api.strip()) < 10:
        raise ToolError("Explain in why_not_api why no API, MCP server or CLI works here (call find_integrations first).")
    async with _start_lock(ctx, agent_id):
        return await _start(ctx, agent_id, why_not_api, start_url, payment_amount_usd, payment_merchant,
                            payment_domains)


async def _start(ctx, agent_id: str, why_not_api: str, start_url: str | None, payment_amount_usd: float | None,
                 payment_merchant: str | None, payment_domains: list[str] | None) -> str:
    handles = _handles(ctx)
    if agent_id in handles:
        h = handles[agent_id]
        if payment_amount_usd is not None and h.sensitive is None:
            raise ToolError("You already have the browser without a payment. Call browser_done, then browser_start "
                            "again with the payment_* arguments to check out.")
        if start_url:
            return await _act("navigate", {"url": start_url}, f"Open {start_url}")
        return await _state(ctx, agent_id, h, "Browser state")
    ctx.emit(agent_id, "status", f"Using the browser: {why_not_api.strip()[:300]}", {"browser_reason": why_not_api.strip()[:300]})

    sensitive = ledger = None
    if payment_amount_usd is not None:
        domains = [clean_domain(d) for d in (payment_domains or [])]
        if not domains or any(not d or "*" in d for d in domains):
            raise ToolError("payment_domains must be exact site domains like ['checkout.stripe.com'] (no wildcards).")
        sensitive = _card_secrets(domains)
        if sensitive is None:
            raise ToolError("No payment card is stored in the vault (Settings → Payment card).")
        ledger = await authorize_spend(
            ctx, amount_usd=float(payment_amount_usd), merchant=payment_merchant or domains[0],
            description=f"{why_not_api.strip()[:160]} (card usable only on: {', '.join(domains)})",
            method="card", agent=agent_id, data={"domains": domains}, require_human=True)

    if ctx.browser_lock.locked():
        ctx.emit(agent_id, "status", "Waiting for the browser (another agent is using it)…")
    try:
        await ctx.browser_lock.acquire()
    except BaseException:
        if ledger is not None:
            settle(ledger, "failed", {"error": "stopped before the checkout started"})
        raise
    try:
        from browser_use import Browser, Tools

        browser = Browser(cdp_url=cdp_url(), keep_alive=True)
        await browser.start()
    except BaseException:
        ctx.browser_lock.release()
        if ledger is not None:
            settle(ledger, "failed", {"error": "browser unavailable"})
        raise
    h = _Handle(browser=browser, tools=Tools(), sensitive=sensitive, ledger=ledger, reason=why_not_api.strip())
    handles[agent_id] = h
    ctx.emit(agent_id, "status", f"Browser task started: {why_not_api.strip()[:200]}", {"browser": "start"})
    if start_url:
        return await _act("navigate", {"url": start_url}, f"Open {start_url}")
    return await _state(ctx, agent_id, h, "Browser state")


@todd_tool(toolset="browser")
async def browser_state() -> str:
    """Current page: URL, tabs, indexed interactive elements and a screenshot."""
    ctx, agent_id, h = _get()
    return await _state(ctx, agent_id, h, "Looked at the page")


@todd_tool(toolset="browser")
async def browser_navigate(url: str, new_tab: bool = False) -> str:
    """Open a URL.

    Args:
        url: full URL
        new_tab: open in a new tab
    """
    return await _act("navigate", {"url": url, "new_tab": new_tab}, f"Open {url}")


@todd_tool(toolset="browser")
async def browser_click(index: int, what: str = "") -> str:
    """Click an element by its [index] from the page state.

    Args:
        index: element index
        what: a few words on what you're clicking (shown to the human)
    """
    return await _act("click", {"index": index}, f"Click {what or f'[{index}]'}")


@todd_tool(toolset="browser")
async def browser_type(index: int, text: str, clear: bool = True, what: str = "") -> str:
    """Type into an input by its [index]. Use <secret>name</secret> placeholders for card fields.

    Args:
        index: element index
        text: text to type
        clear: replace existing text (default) instead of appending
        what: a few words on which field (shown to the human)
    """
    shown = "••••" if "<secret>" in text else (text[:60] + ("…" if len(text) > 60 else ""))
    return await _act("input", {"index": index, "text": text, "clear": clear}, f"Type {shown!r} into {what or f'[{index}]'}")


@todd_tool(toolset="browser")
async def browser_keys(keys: str) -> str:
    """Send keys or shortcuts to the page, e.g. "Enter", "Escape", "Tab", "Control+a".

    Args:
        keys: key or shortcut
    """
    return await _act("send_keys", {"keys": keys}, f"Press {keys}")


@todd_tool(toolset="browser")
async def browser_select(index: int, option: str) -> str:
    """Choose an option of a <select> element by its visible text or value.

    Args:
        index: element index
        option: option text or value
    """
    return await _act("select_dropdown", {"index": index, "text": option}, f"Select {option!r}")


@todd_tool(toolset="browser")
async def browser_scroll(down: bool = True, pages: float = 1.0) -> str:
    """Scroll the page.

    Args:
        down: true to scroll down, false for up
        pages: how far (0.5 = half a screen, 10 = to the end)
    """
    return await _act("scroll", {"down": down, "pages": pages}, f"Scroll {'down' if down else 'up'}")


@todd_tool(toolset="browser")
async def browser_back() -> str:
    """Go back to the previous page."""
    return await _act("go_back", {}, "Back")


@todd_tool(toolset="browser")
async def browser_switch_tab(tab_id: str) -> str:
    """Switch to another open tab (4-character id from the Tabs line).

    Args:
        tab_id: tab id
    """
    return await _act("switch", {"tab_id": tab_id}, f"Switch to tab {tab_id}")


@todd_tool(toolset="browser")
async def browser_search(pattern: str) -> str:
    """Find text on the current page (like grep), with surrounding context. Doesn't change the page.

    Args:
        pattern: text to find
    """
    ctx, agent_id, h = _get()
    if h.sensitive is not None:
        raise ToolError("browser_search is off during a checkout. Use browser_state.")
    r = await h.tools.registry.execute_action("search_page", {"pattern": pattern}, browser_session=h.browser)
    return (getattr(r, "extracted_content", None) or getattr(r, "error", None) or "No matches.").strip()


@todd_tool(toolset="browser")
async def browser_wait(seconds: int = 3) -> str:
    """Wait for the page to update (loading, redirects), then return the new state.

    Args:
        seconds: 1-30
    """
    return await _act("wait", {"seconds": max(1, min(int(seconds), 30))}, f"Wait {seconds}s")


@todd_tool(toolset="browser")
async def browser_done(result: str, success: bool = True) -> str:
    """Release the browser when your browser work is finished (or you can't continue), with what you found or did.

    Args:
        result: what was done / the values found (copy exact values)
        success: whether it worked
    """
    ctx, agent_id = get_ctx(), get_agent_id()
    if agent_id not in _handles(ctx):
        return "You don't have the browser."
    await release(ctx, agent_id, success=success, result=result)
    return "Browser released."


async def release(ctx, agent_id: str, *, success: bool = False, result: str = "") -> None:
    """Give the browser back (called by browser_done and when an agent ends)."""
    h = _handles(ctx).pop(agent_id, None)
    if h is None:
        return
    try:
        if h.ledger is not None:
            settle(h.ledger, "completed" if success else "needs_review", {"result": result[:500]})
        await asyncio.wait_for(h.browser.stop(), 10)
    except BaseException:
        pass
    finally:
        if ctx.browser_lock.locked():
            ctx.browser_lock.release()
        ctx.emit(agent_id, "status", ("Browser task finished" if success else "Browser task stopped")
                 + f" after {h.steps} steps", {"browser": "end"})


DIRECT_BROWSER_TOOLS = [browser_start, browser_state, browser_navigate, browser_click, browser_type, browser_keys,
                        browser_select, browser_scroll, browser_back, browser_switch_tab, browser_search,
                        browser_wait, browser_done]
