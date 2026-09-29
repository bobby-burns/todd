"""Bring a value or a downloaded file from the page straight into the vault, without it passing through the model.

Used by both browser engines (the direct browser tools and the browser-use agent's actions), so an agent can set up a
key that a site shows or downloads once (e.g. an App Store Connect .p8 key) while the human only signs in.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..sdk import ToolError

MAX_FILE_BYTES = 64 * 1024  # keys and config files, not arbitrary downloads

# Copies an element's full text (see browser_read_text); mode "value" = the one value a key field or copy button
# stands for (see browser_save_secret).
TEXT_JS = """function(mode) {
  const root = (this && this.nodeType) ? this : document.body;
  const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const block = /^(ADDRESS|ARTICLE|ASIDE|BLOCKQUOTE|BR|DD|DIV|DL|DT|FIELDSET|FIGCAPTION|FIGURE|FOOTER|FORM|H[1-6]|HEADER|HR|LI|MAIN|NAV|OL|P|PRE|SECTION|TABLE|TR|UL)$/;
  const field = (el) => {
    if (el.tagName === 'TEXTAREA') return el.value;
    if (el.tagName !== 'INPUT') return null;
    const t = (el.type || 'text').toLowerCase();
    return ['password', 'hidden', 'checkbox', 'radio', 'file', 'submit', 'button', 'image'].includes(t) ? '' : el.value;
  };
  if (mode === 'value') {
    const v = field(root);
    if (v !== null) return v;
    if (root.tagName === 'CLIPBOARD-COPY') {
      if (root.getAttribute('value')) return root.getAttribute('value');
      const id = root.getAttribute('for');
      const target = id && ((root.getRootNode().getElementById && root.getRootNode().getElementById(id)) || document.getElementById(id));
      if (target) return field(target) ?? target.textContent;
    }
  }
  const out = [];
  const push = (t) => {  // collapsed text: no space at the start of a line or after another space
    const last = out.length ? out[out.length - 1] : '\\n';
    if (last.endsWith('\\n') || last.endsWith(' ')) t = t.replace(/^ +/, '');
    if (t) out.push(t);
  };
  const walk = (n) => {
    if (n.nodeType === 3) {
      if (n.parentElement && n.parentElement.closest('pre, textarea')) out.push(n.nodeValue);
      else push(n.nodeValue.replace(/\\s+/g, ' '));
      return;
    }
    if (n.nodeType !== 1 && n.nodeType !== 11) return;
    if (n.nodeType === 1) {
      if (skip.has(n.tagName) || n.hidden) return;
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden') return;
      const v = field(n);
      if (v !== null) { if (v) push(' ' + v + ' '); return; }
      if (n.tagName === 'TD' || n.tagName === 'TH') out.push('\\t');
    }
    const kids = n.shadowRoot ? [n.shadowRoot]
      : n.tagName === 'SLOT' && n.assignedNodes({flatten: true}).length ? n.assignedNodes({flatten: true})
      : [...n.childNodes];
    for (const c of kids) walk(c);
    if (n.nodeType === 1 && block.test(n.tagName)) out.push('\\n');
  };
  walk(root);
  return out.join('').split('\\n').map(l => l.replace(/[ \\t]+$/, '')).join('\\n')
    .replace(/\\n{3,}/g, '\\n\\n').trim();
}"""

# Catch a download the page starts (a link with `download`, or a blob:/data: URL made by script) and keep its
# contents in the page instead of writing it to the browser's Downloads folder.
DOWNLOAD_HOOK_JS = r"""(() => {
  window.__todd_dl = null;
  if (window.__todd_dl_unhook) return;
  const blobs = new Map();
  const create = URL.createObjectURL, click = HTMLAnchorElement.prototype.click,
        dispatch = HTMLAnchorElement.prototype.dispatchEvent;
  URL.createObjectURL = function (o) {
    const u = create.apply(this, arguments);
    try { if (o instanceof Blob) blobs.set(u, o); } catch (e) {}
    return u;
  };
  const isDownload = (a) => a && a.tagName === 'A' && (a.hasAttribute('download') || /^(blob|data):/.test(a.href || ''));
  const grab = async (a) => {
    const href = a.href || '';
    const name = a.getAttribute('download') || decodeURIComponent((href.split('/').pop() || '').split('?')[0]);
    try {
      let text;
      if (href.startsWith('blob:') && blobs.has(href)) text = await blobs.get(href).text();
      else if (href.startsWith('data:')) {
        const i = href.indexOf(','), meta = href.slice(5, i), body = href.slice(i + 1);
        text = /;base64/i.test(meta) ? atob(body) : decodeURIComponent(body);
      } else text = await (await fetch(href, {credentials: 'include'})).text();
      window.__todd_dl = {name, text};
    } catch (e) {
      window.__todd_dl = {name, error: String(e)};
    }
  };
  const onClick = (e) => {
    const a = e.target && e.target.closest && e.target.closest('a');
    if (isDownload(a)) { e.preventDefault(); e.stopImmediatePropagation(); grab(a); }
  };
  document.addEventListener('click', onClick, true);
  HTMLAnchorElement.prototype.click = function () {
    if (isDownload(this)) { grab(this); return; }
    return click.apply(this, arguments);
  };
  HTMLAnchorElement.prototype.dispatchEvent = function (ev) {
    if (ev && ev.type === 'click' && isDownload(this)) { grab(this); return true; }
    return dispatch.apply(this, arguments);
  };
  // Put everything back once the file is caught (or given up on): later downloads, the human's included, work normally.
  window.__todd_dl_unhook = () => {
    URL.createObjectURL = create;
    HTMLAnchorElement.prototype.click = click;
    HTMLAnchorElement.prototype.dispatchEvent = dispatch;
    document.removeEventListener('click', onClick, true);
    blobs.clear();
    delete window.__todd_dl_unhook;
  };
})()"""

# Take the caught file out of the page in one step: read it, clear it, remove the hook.
TAKE_JS = r"""(() => {
  const d = window.__todd_dl;
  if (!d) return null;
  window.__todd_dl = null;
  if (window.__todd_dl_unhook) window.__todd_dl_unhook();
  return JSON.stringify(d);
})()"""
UNHOOK_JS = "window.__todd_dl = null; if (window.__todd_dl_unhook) window.__todd_dl_unhook();"


async def _resolve(browser: Any, index: int | None) -> tuple[Any, str]:
    if index is None:
        s = await browser.get_or_create_cdp_session(focus=False)
        doc = await s.cdp_client.send.Runtime.evaluate(params={"expression": "document.body"}, session_id=s.session_id)
        return s, doc["result"]["objectId"]
    node = await browser.get_element_by_index(int(index))
    if node is None:
        raise ToolError(f"No element [{index}] on the current page. Refresh the page state to get current indexes.")
    s = await browser.cdp_client_for_node(node)
    res = await s.cdp_client.send.DOM.resolveNode(params={"backendNodeId": node.backend_node_id},
                                                  session_id=s.session_id)
    return s, res["object"]["objectId"]


async def element_text(browser: Any, index: int | None, mode: str = "text") -> str:
    s, object_id = await _resolve(browser, index)
    r = await s.cdp_client.send.Runtime.callFunctionOn(
        params={"objectId": object_id, "functionDeclaration": TEXT_JS, "arguments": [{"value": mode}],
                "returnByValue": True}, session_id=s.session_id)
    return str(r.get("result", {}).get("value") or "")


async def capture_download(browser: Any, index: int, timeout: float = 20) -> tuple[str, str]:
    """Click the element that starts a download and return (file name, contents), without saving the file."""
    s, object_id = await _resolve(browser, index)
    send = s.cdp_client.send
    await send.Runtime.evaluate(params={"expression": DOWNLOAD_HOOK_JS}, session_id=s.session_id)
    await send.Runtime.callFunctionOn(params={"objectId": object_id, "functionDeclaration": "function(){ this.click(); }"},
                                      session_id=s.session_id)
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        r = await send.Runtime.evaluate(params={"expression": TAKE_JS, "returnByValue": True}, session_id=s.session_id)
        raw = r.get("result", {}).get("value")
        got = json.loads(raw) if isinstance(raw, str) and raw != "null" else None
        if got:
            if got.get("error"):
                raise ToolError(f"The download failed in the page: {got['error']}")
            return str(got.get("name") or "download"), str(got.get("text") or "")
        await asyncio.sleep(0.5)
    try:
        await send.Runtime.evaluate(params={"expression": UNHOOK_JS}, session_id=s.session_id)
    except Exception:  # noqa: BLE001
        pass
    raise ToolError("No download was caught. If clicking opened a confirmation, point at its Download button and call "
                    "this again; if the site downloads from its server in a way Todd can't catch, ask_human to add "
                    "the file in Settings → Vault.")


def check_name(name: str) -> None:
    import re

    from .. import vault

    if not re.fullmatch(r"[A-Z0-9_]{2,64}", name):
        raise ToolError("Secret names must be UPPER_SNAKE_CASE")
    if vault.is_protected(name):
        raise ToolError(f"{name} is an integration token, model key or card field: agents can't set it. Connect the "
                        "service with cli_login, or ask the human to add it in Settings.")


def save_file(name: str, file_name: str, text: str) -> str:
    from .. import vault

    body = text.strip()
    if not body:
        raise ToolError(f"The downloaded file ({file_name}) is empty.")
    if len(body.encode()) > MAX_FILE_BYTES:
        raise ToolError(f"The downloaded file is larger than {MAX_FILE_BYTES // 1024} KB; only keys and small config "
                        "files go in the vault.")
    if "�" in body:
        raise ToolError("The downloaded file isn't text (a key or config file); not saving it.")
    vault.set_secret(name, body)
    return (f"Saved {file_name} ({len(body)} characters) to the vault as {name} without showing it to you. "
            f"Reference it as {{{{secret:{name}}}}}.")
