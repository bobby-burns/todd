"""The `web` toolset: fetch a public URL as text (docs, APIs, pages that don't need a login)."""

from __future__ import annotations

import html
import re

import httpx

from ..sdk import ToolError, todd_tool

_TAGS = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


# Todd's own services and machine-local addresses: agents never reach these through the web tools (a page or an
# API could otherwise point them at the browser's debugging port, the sandbox, or a cloud metadata endpoint).
INTERNAL_HOSTS = {"localhost", "sandbox", "browser", "postgres", "api", "web", "ollama", "metadata.google.internal",
                  "metadata", "host.docker.internal"}


async def check_url(url: str) -> str:
    """The URL's host, or ToolError if it points at Todd's internal services or a local/metadata address."""
    import asyncio
    import ipaddress
    import os
    import socket
    from urllib.parse import urlparse

    if not url.startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        raise ToolError("url has no host")
    if host in INTERNAL_HOSTS or host.endswith(".localhost") or host.endswith(".internal"):
        raise ToolError(f"{host} is an internal address; the web tools only reach the public internet")
    allow_private = os.getenv("TODD_ALLOW_PRIVATE_URLS") == "1"
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return host  # doesn't resolve: the request itself will fail
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or \
                (ip.is_private and not allow_private):
            raise ToolError(f"{host} resolves to a private address ({ip}); the web tools only reach the public "
                            "internet (set TODD_ALLOW_PRIVATE_URLS=1 to allow your own network)")
    return host


async def _send(c: httpx.AsyncClient, method: str, url: str, *, follow: bool, **kw) -> httpx.Response:
    """Send, following redirects only to public hosts (and not at all when a secret was injected: a redirect must
    not carry your key to another site)."""
    for _ in range(6):
        r = await c.request(method, url, **kw)
        if not (follow and r.is_redirect and r.headers.get("location")):
            return r
        url = str(r.url.join(r.headers["location"]))
        await check_url(url)
        if r.status_code in (301, 302, 303):
            method, kw = "GET", {k: v for k, v in kw.items() if k == "headers"}
    return r


@todd_tool(toolset="web", planner=True)
async def fetch_url(url: str, max_chars: int = 20000) -> dict:
    """Fetch a public web page or API endpoint (no login) and return its text. Faster and cheaper than the
    browser for docs, READMEs, JSON APIs and simple pages.

    Args:
        url: http(s) URL
        max_chars: maximum characters of text to return (default 20000)
    """
    await check_url(url)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0 (Todd agent)"}) as c:
        r = await _send(c, "GET", url, follow=True)
    ctype = r.headers.get("content-type", "")
    text = r.text
    if "html" in ctype:
        text = html.unescape(_TAGS.sub(" ", text))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return {"status": r.status_code, "url": str(r.url), "content_type": ctype.split(";")[0],
            "text": text[: max(1000, min(int(max_chars), 100000))]}


# Protected secrets may be sent only to their own service's API host.
SECRET_HOSTS = {"GITHUB_TOKEN": {"api.github.com", "uploads.github.com"}, "VERCEL_TOKEN": {"api.vercel.com"},
                "EXPO_TOKEN": {"api.expo.dev"}}
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
                      form: dict[str, str] | None = None, max_chars: int = 20000,
                      save_to_vault: dict[str, str] | None = None) -> dict:
    """Call a REST/GraphQL API directly — the preferred way to work with any service that has an API. Put vault
    secrets in headers or body as {{secret:NAME}} (e.g. {"Authorization": "Bearer {{secret:STRIPE_SECRET_KEY}}"});
    values are injected here and never shown to you. Use find_integrations to learn which API and key to use.
    When the response contains a new secret (an OAuth token exchange, a "create API key" call), pass save_to_vault
    so it goes straight into the vault instead of into your context.

    Args:
        method: GET, POST, PUT, PATCH or DELETE
        url: full https URL
        headers: request headers (may contain {{secret:NAME}})
        json_body: JSON body (strings may contain {{secret:NAME}})
        form: form-encoded body instead of JSON
        max_chars: maximum characters of the response to return
        save_to_vault: {"NAME": "path.in.the.json.response"}, e.g. {"STRIPE_RESTRICTED_KEY": "secret"} or
            {"SERVICE_TOKEN": "data.0.token"}; each value is stored and shown to you as {{secret:NAME}}
    """
    import json as _json

    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ToolError("method must be GET, POST, PUT, PATCH or DELETE")
    host = await check_url(url)
    hdrs = {k: _inject(str(v), host) for k, v in (headers or {}).items()}
    body = _json.loads(_inject(_json.dumps(json_body), host)) if json_body is not None else None
    data = {k: _inject(str(v), host) for k, v in form.items()} if form else None
    injected = bool(_REF.search(_json.dumps([headers, json_body, form])))
    async with httpx.AsyncClient(timeout=60) as c:
        r = await _send(c, method, url, follow=not injected, headers=hdrs, json=body, data=data)
    try:
        payload: object = r.json()
    except Exception:
        payload = None
    saved = _save_fields(payload, save_to_vault) if save_to_vault else []
    text = _json.dumps(payload, indent=1) if payload is not None else r.text
    out: dict = {"status": r.status_code, "ok": r.is_success, "body": text[: max(1000, min(int(max_chars), 100000))]}
    if injected and r.is_redirect:
        out["note"] = "Not following the redirect: the request carried a vault secret."
    if saved:
        out["saved_to_vault"] = saved
    return out


def _save_fields(payload: object, fields: dict[str, str]) -> list[str]:
    """Store response fields in the vault and replace them in `payload` with {{secret:NAME}} (in place)."""
    from .. import vault
    from .page_capture import check_name

    saved = []
    for name, path in fields.items():
        check_name(name)
        parent, key, cur = None, None, payload
        for part in [p for p in str(path).split(".") if p]:
            parent, key = cur, (int(part) if part.isdigit() and isinstance(cur, list) else part)
            try:
                cur = cur[key]  # type: ignore[index]
            except (KeyError, IndexError, TypeError):
                raise ToolError(f"The response has no {path!r} to save as {name}.") from None
        if not isinstance(cur, (str, int)) or not str(cur):
            raise ToolError(f"{path!r} isn't a single value (can't save it as {name}).")
        vault.set_secret(name, str(cur))
        if parent is not None:
            parent[key] = f"{{{{secret:{name}}}}}"  # type: ignore[index]
        saved.append(name)
    return saved


WEB_TOOLS = [fetch_url, api_request]
