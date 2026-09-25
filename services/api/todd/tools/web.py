"""The `web` toolset: fetch a public URL as text (docs, APIs, pages that don't need a login)."""

from __future__ import annotations

import html
import re

import httpx

from ..sdk import ToolError, todd_tool

_TAGS = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


@todd_tool(toolset="web", planner=True)
async def fetch_url(url: str, max_chars: int = 20000) -> dict:
    """Fetch a public web page or API endpoint (no login) and return its text. Faster and cheaper than the
    browser for docs, READMEs, JSON APIs and simple pages.

    Args:
        url: http(s) URL
        max_chars: maximum characters of text to return (default 20000)
    """
    if not url.startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (Todd agent)"}) as c:
        r = await c.get(url)
    ctype = r.headers.get("content-type", "")
    text = r.text
    if "html" in ctype:
        text = html.unescape(_TAGS.sub(" ", text))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return {"status": r.status_code, "url": str(r.url), "content_type": ctype.split(";")[0],
            "text": text[: max(1000, min(int(max_chars), 100000))]}


# Protected secrets may be sent only to their own service's API host.
SECRET_HOSTS = {"GITHUB_TOKEN": {"api.github.com", "uploads.github.com"}, "VERCEL_TOKEN": {"api.vercel.com"}}
_REF = re.compile(r"\{\{secret:([A-Za-z0-9_\-]+)\}\}")


def _inject(value: str, host: str) -> str:
    from .. import vault

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if vault.is_protected(name) and host not in SECRET_HOSTS.get(name, set()):
            raise ToolError(f"{name} can only be sent to {sorted(SECRET_HOSTS.get(name, [])) or 'its own tools'}")
        v = vault.get_secret(name)
        if v is None:
            raise ToolError(f"secret {name!r} is not in the vault")
        return v

    return _REF.sub(sub, value)


@todd_tool(toolset="web", planner=True)
async def api_request(method: str, url: str, headers: dict[str, str] | None = None, json_body: dict | list | None = None,
                      form: dict[str, str] | None = None, max_chars: int = 20000) -> dict:
    """Call a REST/GraphQL API directly — the preferred way to work with any service that has an API. Put vault
    secrets in headers or body as {{secret:NAME}} (e.g. {"Authorization": "Bearer {{secret:STRIPE_SECRET_KEY}}"});
    values are injected here and never shown to you. Use find_integrations to learn which API and key to use.

    Args:
        method: GET, POST, PUT, PATCH or DELETE
        url: full https URL
        headers: request headers (may contain {{secret:NAME}})
        json_body: JSON body (strings may contain {{secret:NAME}})
        form: form-encoded body instead of JSON
        max_chars: maximum characters of the response to return
    """
    import json as _json
    from urllib.parse import urlparse

    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ToolError("method must be GET, POST, PUT, PATCH or DELETE")
    if not url.startswith(("https://", "http://")):
        raise ToolError("url must be http(s)")
    host = (urlparse(url).hostname or "").lower()
    hdrs = {k: _inject(str(v), host) for k, v in (headers or {}).items()}
    body = _json.loads(_inject(_json.dumps(json_body), host)) if json_body is not None else None
    data = {k: _inject(str(v), host) for k, v in form.items()} if form else None
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.request(method, url, headers=hdrs, json=body, data=data)
    try:
        payload: object = r.json()
        text = _json.dumps(payload, indent=1)
    except Exception:
        text = r.text
    return {"status": r.status_code, "ok": r.is_success, "body": text[: max(1000, min(int(max_chars), 100000))]}


WEB_TOOLS = [fetch_url, api_request]
