"""Apify client for the trend scan: run an actor, wait, read its dataset, fetch files it stored.

The token (APIFY_API_TOKEN, from the vault) is only ever sent to api.apify.com: files the actor stored (subtitles,
videos, covers) are fetched from Apify's own storage, never from a URL some page or result could point elsewhere."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..sdk import ToolError

API = "https://api.apify.com/v2"
HOST = "api.apify.com"
TIKTOK = "clockworks~tiktok-scraper"
# USD per event on Apify's FREE tier (2026-10-06): an upper bound, paid plans pay less
PRICES: dict[str, dict[str, float]] = {
    TIKTOK: {"actor-start": 0.001, "result": 0.0037, "video-download": 0.0013},
}
ASK_KEY = ("Todd needs your Apify API token to find what's trending in this niche (a scan costs about $0.05–0.25; "
           "Apify's free plan includes $5 a month). Sign in at https://console.apify.com, copy the token from "
           "Settings → API & Integrations (https://console.apify.com/settings/integrations) and paste it here: it "
           "goes straight into Todd's vault.")
DONE = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")


def estimate(actor: str, results: int, downloads: int = 0) -> float:
    p = PRICES.get(actor, PRICES[TIKTOK])
    return p["actor-start"] + results * p["result"] + downloads * p.get("video-download", 0)


async def _call(token: str, method: str, path: str, timeout: float = 90, **kw: Any) -> Any:
    try:
        async with httpx.AsyncClient(base_url=API, timeout=timeout) as c:
            r = await c.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
    except httpx.HTTPError as e:
        raise ToolError(f"Apify unreachable: {e}") from e
    if r.status_code in (401, 403) and "limit" not in r.text.lower():
        raise ToolError("Apify refused APIFY_API_TOKEN: ask the human to check it in Settings → Vault")
    if r.status_code == 402 or "usage limit" in r.text.lower() or "insufficient" in r.text.lower():
        raise ToolError("the Apify account is out of credit for this month: ask the human to top it up")
    if r.status_code >= 400:
        raise ToolError(f"Apify {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


async def run_actor(token: str, actor: str, payload: dict[str, Any], max_wait: float = 600) -> tuple[dict, list]:
    """Start the actor, wait for it (Apify answers at most 60 s at a time), return (run, dataset items)."""
    run = (await _call(token, "POST", f"/acts/{actor}/runs", json=payload))["data"]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while run["status"] not in DONE:
        if loop.time() > deadline:
            await _call(token, "POST", f"/actor-runs/{run['id']}/abort")
            raise ToolError(f"Apify {actor} took longer than {max_wait:.0f}s; stopped it")
        run = (await _call(token, "GET", f"/actor-runs/{run['id']}", params={"waitForFinish": 60}, timeout=90))["data"]
    if run["status"] != "SUCCEEDED":
        raise ToolError(f"Apify {actor} {run['status'].lower()}: {run.get('statusMessage') or ''}"[:300])
    items = await _call(token, "GET", f"/datasets/{run['defaultDatasetId']}/items", params={"clean": "true"},
                        timeout=120)
    return run, list(items or [])


def stored(url: str | None) -> bool:
    """A file in Apify's own storage (the only place the token may go)."""
    if not url:
        return False
    u = urlsplit(url)
    return u.scheme == "https" and (u.hostname or "").lower() == HOST and u.path.startswith("/v2/key-value-stores/")


async def fetch(token: str, url: str, max_bytes: int = 25_000_000) -> bytes:
    """A file an actor stored (subtitles, a video, a cover)."""
    if not stored(url):
        raise ToolError(f"not a file in Apify's storage: {url[:120]}")
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as c:
            async with c.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as r:
                if r.status_code >= 400:
                    raise ToolError(f"Apify storage -> {r.status_code}")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise ToolError("file too large")
                return bytes(buf)
    except httpx.HTTPError as e:
        raise ToolError(f"Apify storage unreachable: {e}") from e
