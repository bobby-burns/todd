"""Keep what a page shows from leaking secrets into the model (both browser engines).

* Screenshots: right before the browser captures the page, everything that looks like a secret (the same patterns as
  redact.page: API keys, tokens, private keys, JWTs, long random strings) is blurred on screen, then restored. The
  model, the timeline and saved screenshots only ever see the blur.
* Text: page state, read text, search results, console output and step notes go through redact.page / redact.urls.
* browser-use's own agent (API engine): its state messages are redacted the same way before its LLM sees them.

Agents that need such a value save it with browser_save_secret / save_to_vault, which reads it straight from the
page into the vault.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .. import redact

SHIELD_JS = """(async (sources) => {
  const res = sources.map((s) => new RegExp(s));
  const hit = (t) => !!t && t.length >= 20 && res.some((r) => r.test(t));
  const marked = [];
  const mark = (el) => {
    if (!el || !el.style || el.dataset.toddShield !== undefined) return;
    el.dataset.toddShield = el.style.filter || '';
    el.style.filter = 'blur(8px)';
    marked.push(el);
  };
  const walk = (root) => {
    if (!root) return;
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let n;
    while ((n = tw.nextNode())) {
      if (n.nodeType === 3) { if (hit(n.nodeValue)) mark(n.parentElement); continue; }
      if ((n.tagName === 'INPUT' || n.tagName === 'TEXTAREA') && n.type !== 'password' && hit(n.value)) mark(n);
      if (n.shadowRoot) walk(n.shadowRoot);
    }
  };
  try { walk(document.body || document.documentElement); } catch (e) {}
  for (const f of document.querySelectorAll('iframe')) { try { walk(f.contentDocument && f.contentDocument.body); } catch (e) {} }
  window.__toddShielded = marked;
  if (marked.length) await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  return marked.length;
})"""

UNSHIELD_JS = """(() => {
  for (const el of window.__toddShielded || []) {
    try { el.style.filter = el.dataset.toddShield || ''; delete el.dataset.toddShield; } catch (e) {}
  }
  window.__toddShielded = [];
})()"""

NOTE = ("Parts of this page look like secrets: they're blurred in the screenshot and hidden in the text. To keep one, "
        "save it with browser_save_secret (it goes straight to the vault).")


async def _eval(browser: Any, expression: str) -> Any:
    try:
        s = await browser.get_or_create_cdp_session(focus=False)
        r = await asyncio.wait_for(s.cdp_client.send.Runtime.evaluate(
            params={"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=s.session_id), 5)
        return r.get("result", {}).get("value")
    except Exception:  # noqa: BLE001  (no page yet, a page that blocks scripts: nothing to blur)
        return None


async def shield(browser: Any) -> int:
    return int(await _eval(browser, f"({SHIELD_JS})({json.dumps(redact.js_sources())})") or 0)


async def unshield(browser: Any) -> None:
    await _eval(browser, UNSHIELD_JS)


def install(browser: Any) -> None:
    """Blur secret-looking text whenever this browser session captures the page with a screenshot."""
    if getattr(browser, "_todd_guarded", False):
        return
    original = browser.get_browser_state_summary

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        wants_shot = kwargs.get("include_screenshot", args[0] if args else True)
        n = await shield(browser) if wants_shot else 0
        try:
            return await original(*args, **kwargs)
        finally:
            if n:
                await unshield(browser)
            object.__setattr__(browser, "_todd_shielded", n)

    object.__setattr__(browser, "get_browser_state_summary", guarded)
    object.__setattr__(browser, "_todd_guarded", True)


def shielded(browser: Any) -> int:
    """How many elements were blurred in the last capture."""
    return int(getattr(browser, "_todd_shielded", 0) or 0)


def guard_agent(agent: Any) -> None:
    """browser-use's agent: redact every state message (page text, action results) before its LLM sees it."""
    mm = getattr(agent, "_message_manager", None)
    if mm is None or getattr(mm, "_todd_guarded", False):
        return
    original = mm._set_message_with_type

    def patched(message: Any, message_type: str) -> Any:
        if message_type == "state":
            content = getattr(message, "content", None)
            if isinstance(content, str):
                message.content = redact.page(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(getattr(part, "text", None), str):
                        part.text = redact.page(part.text)
        return original(message, message_type)

    object.__setattr__(mm, "_set_message_with_type", patched)
    object.__setattr__(mm, "_todd_guarded", True)
