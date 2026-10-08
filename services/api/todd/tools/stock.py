"""Stock video for shorts: b-roll of places, objects, hands and people doing things, from Pixabay or Pexels (free keys).

  short_stock       search, and see a sheet of the candidates (a frame each, numbered, with length and size)
  short_stock_pick  download one into video/stock/<name>.mp4, credit it in video/stock/CREDITS.md, and see its frames

Both sites' terms are followed: results are cached (Pixabay asks for 24 hours), clips are downloaded rather than
hotlinked (Pixabay's links expire, so a pick looks its clip up again first), and every clip used is credited. The
media service does the downloading, from its allow-list of hosts only; the keys never leave the API.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Any

import httpx

from .. import vault
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from . import media, sandbox
from .human import secret_or_ask
from .sandbox_tools import _abs, _root

PIXABAY_VIDEOS = "https://pixabay.com/api/videos/"
PEXELS_VIDEOS = "https://api.pexels.com/videos/search"
CACHE_S = 24 * 3600
STOCK_DIR = "video/stock"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
MAX_CLIP_S = 60
ASK_PIXABAY = ("Todd can use free stock video for b-roll (places, hands, people doing things) in your shorts. It needs "
               "a free Pixabay API key: sign up at https://pixabay.com/accounts/register/, then copy the key shown on "
               "https://pixabay.com/api/docs/ (under \"Parameters\") and paste it here: it goes straight into Todd's "
               "vault. Or skip, and the short uses screen recordings and text only.")
_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_locks: dict[str, asyncio.Lock] = {}  # one per run: producers side by side share video/stock/searches.json


def _emit(text: str, data: dict[str, Any]) -> None:
    get_ctx().emit(get_agent_id(), "step", text, data)


async def _get(url: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    site = "Pixabay" if "pixabay" in url else "Pexels"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise ToolError(f"{site} unreachable: {e}") from e
    if r.status_code in (400, 401, 403) and ("key" in r.text.lower() or r.status_code != 400):
        raise ToolError(f"{site} refused its API key: ask the human to check it in Settings → Vault")
    if r.status_code == 429:
        raise ToolError(f"{site}'s rate limit is used up: try again in a minute")
    if r.status_code >= 400:
        raise ToolError(f"{site} video search -> {r.status_code}: {r.text[:300]}")
    return r.json()


def _from_pixabay(h: dict[str, Any]) -> dict[str, Any] | None:
    vids = h.get("videos") or {}
    files = [v for v in (vids.get(k) or {} for k in ("large", "medium", "small")) if v.get("url")]
    if not h.get("id") or not files:
        return None
    portrait = any(int(f.get("height") or 0) > int(f.get("width") or 0) for f in files)
    # the smallest file that is sharp enough once it fills a 1080×1920 frame
    want = 1920 if portrait else 1080
    best = next((f for f in sorted(files, key=lambda f: int(f.get("height") or 0)) if int(f.get("height") or 0) >=
                 want), max(files, key=lambda f: int(f.get("height") or 0)))
    user, uid = h.get("user"), h.get("user_id")
    thumb = next((v.get("thumbnail") for v in (vids.get(k) or {} for k in ("medium", "small", "large"))
                  if v.get("thumbnail")), None)
    return {"key": f"pixabay:{h['id']}", "duration_s": h.get("duration"), "width": best.get("width"),
            "height": best.get("height"), "portrait": portrait, "thumb": thumb, "tags": h.get("tags"),
            "page_url": h.get("pageURL"), "creator": user,
            "creator_url": f"https://pixabay.com/users/{user}-{uid}/" if user and uid else None, "file": best["url"]}


def _from_pexels(v: dict[str, Any]) -> dict[str, Any] | None:
    files = [f for f in v.get("video_files") or [] if f.get("link") and f.get("file_type") == "video/mp4"]
    if not v.get("id") or not files:
        return None
    portrait = int(v.get("height") or 0) > int(v.get("width") or 0)
    want = 1920 if portrait else 1080
    sized = sorted(files, key=lambda f: int(f.get("height") or 0))
    best = next((f for f in sized if int(f.get("height") or 0) >= want), sized[-1])
    user = v.get("user") or {}
    return {"key": f"pexels:{v['id']}", "duration_s": v.get("duration"), "width": best.get("width"),
            "height": best.get("height"), "portrait": portrait, "thumb": v.get("image"), "tags": None,
            "page_url": v.get("url"), "creator": user.get("name"), "creator_url": user.get("url"),
            "file": best["link"]}


async def search(source: str, key: str, query: str, n: int = 20) -> list[dict[str, Any]]:
    """Candidates from one site, cached for 24 hours (Pixabay's terms ask for it)."""
    now = time.time()
    hit = _cache.get((source, query))
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    if source == "pixabay":
        data = await _get(PIXABAY_VIDEOS, {"key": key, "q": query[:100], "video_type": "film", "safesearch": "true",
                                           "per_page": max(3, min(n, 50))})
        found = [_from_pixabay(h) for h in data.get("hits") or []]
    else:
        data = await _get(PEXELS_VIDEOS, {"query": query, "orientation": "portrait", "per_page": min(n, 40)},
                          {"Authorization": key})
        found = [_from_pexels(v) for v in data.get("videos") or []]
    out = [c for c in found if c and float(c.get("duration_s") or 0) <= MAX_CLIP_S]
    for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_S]:
        del _cache[k]
    _cache[(source, query)] = (now, out)
    return out


async def _key(source: str | None) -> tuple[str, str]:
    """(site, key): the site asked for, else whichever key the vault has; with neither, the tool asks the human for a
    free Pixabay key by itself."""
    pix, pex = vault.get_secret("PIXABAY_API_KEY"), vault.get_secret("PEXELS_API_KEY")
    if source == "pexels":
        if not pex:
            raise ToolError("PEXELS_API_KEY isn't in the vault (Pexels has paused new keys): use source=\"pixabay\"")
        return "pexels", pex
    if source is None and pex and not pix:
        return "pexels", pex
    return "pixabay", pix or await secret_or_ask("PIXABAY_API_KEY", ASK_PIXABAY)


async def _read_text(path: str) -> str:
    try:
        r = await sandbox.call("/files/view", {"base": _root(), "path": path, "raw": True}, timeout=30)
    except ToolError as e:
        if "-> 404" in str(e):
            return ""
        raise
    return base64.b64decode(r["data"]).decode("utf-8", "replace")


async def _searches(run_id: str) -> dict[str, Any]:
    from .shorts import _read_json

    state = (await _read_json(f"{STOCK_DIR}/searches.json")) or {}
    return {"candidates": state.get("candidates") or {},
            "last": state.get("last") if isinstance(state.get("last"), dict) else {}}  # agent id -> its last search


@todd_tool(toolset="shorts")
async def short_stock(query: str, source: str | None = None, k: int = 8) -> dict:
    """Find stock video for a shot the product's screens can't give: a place, an object, hands doing something, a
    person doing something (not talking). You see a numbered sheet of the candidates with this result; pick one with
    short_stock_pick. Search the way a phone video would be described, plain and concrete ("hands lacing hockey
    skates", "empty ice rink locker room", "pov typing on laptop at night"), never "business" or "success"; skip
    glossy, posed, slow-motion or drone shots and anything with a watermark or text: they read as stock. Portrait
    clips fill the frame best.

    Args:
        query: what the shot shows, in a few plain words
        source: "pixabay" or "pexels" (default: whichever key the vault has; a free Pixabay key is asked for)
        k: how many candidates to show (2–12)
    """
    q = " ".join(str(query or "").split())
    if not 2 <= len(q) <= 100:
        raise ToolError("query is a few plain words (up to 100 characters)")
    if source not in (None, "pixabay", "pexels"):
        raise ToolError("source is \"pixabay\" or \"pexels\"")
    site, key = await _key(source)
    ctx = get_ctx()
    found = await search(site, key, q)
    found.sort(key=lambda c: (not c["portrait"], -int(c.get("height") or 0)))
    shown = found[:max(2, min(int(k), 12))]
    if not shown:
        return {"candidates": [], "next": "nothing found: try plainer words, or another source"}
    from .shorts import _write_json

    async with _locks.setdefault(ctx.run_id, asyncio.Lock()):
        state = await _searches(ctx.run_id)
        for c in shown:
            state["candidates"][c["key"]] = c
        state["last"][get_agent_id()] = [c["key"] for c in shown]
        await _write_json(f"{STOCK_DIR}/searches.json", state)
    labels = [f"{n} · {float(c.get('duration_s') or 0):.0f}s · {'portrait' if c['portrait'] else 'landscape'}"
              for n, c in enumerate(shown, 1)]
    try:  # one tile per candidate, in order (a missing preview is a blank tile, so the numbers stay right)
        r = await media.call("/thumbs", {"run_id": ctx.run_id, "urls": [c.get("thumb") or "" for c in shown],
                                         "labels": labels, "out": f"{STOCK_DIR}/search-{int(time.time())}.png"})
        ctx.push_image(get_agent_id(), r["png_b64"], "image/png")
    except ToolError:
        pass  # the list still says what each one is
    _emit(f"Stock video for \"{q}\": {len(shown)} candidates", {"query": q, "source": site})
    return {"source": site, "candidates": [
        {"n": n, "key": c["key"], "seconds": c.get("duration_s"), "size": f"{c.get('width')}×{c.get('height')}",
         "portrait": c["portrait"], "tags": c.get("tags")} for n, c in enumerate(shown, 1)],
        "next": "look at the sheet; short_stock_pick the one a person could have filmed on their phone (or search "
                "again with other words)"}


@todd_tool(toolset="shorts")
async def short_stock_pick(pick: str, name: str) -> dict:
    """Download a stock clip from short_stock into video/stock/<name>.mp4 (credited in video/stock/CREDITS.md) and
    see a few frames of it, to choose in_s. Use it as a shot: {"source": "clip", "path": "video/stock/<name>.mp4",
    "in_s": …}: take 1–2 s from the middle, where something happens. It's toned down to sit with phone footage.

    Args:
        pick: the candidate's number in your last search (e.g. "3") or its key (e.g. "pixabay:12345")
        name: a short name for the clip, e.g. "laces-skates"
    """
    if not NAME.fullmatch(name or ""):
        raise ToolError("name is lowercase letters, digits and dashes, e.g. \"laces-skates\"")
    ctx = get_ctx()
    state = await _searches(ctx.run_id)
    last = state["last"].get(get_agent_id()) or []
    key = last[int(pick) - 1] if str(pick).isdigit() and 0 < int(pick) <= len(last) else str(pick)
    c = state["candidates"].get(key)
    if c is None:
        raise ToolError("pick is a number from the last short_stock search, or a key it returned")
    site = key.split(":", 1)[0]
    if site == "pixabay":  # its links expire: look the clip up again for a fresh one
        _, k = await _key("pixabay")
        data = await _get(PIXABAY_VIDEOS, {"key": k, "id": key.split(":", 1)[1]})
        fresh = _from_pixabay((data.get("hits") or [{}])[0])
        if fresh:
            c = {**c, "file": fresh["file"]}
    out = f"{STOCK_DIR}/{name}.mp4"
    r = await media.call("/fetch/video", {"run_id": ctx.run_id, "url": c["file"], "out": out}, timeout=180)
    from .shorts import _sheet

    credits = await _read_text(f"{STOCK_DIR}/CREDITS.md")
    if not credits:
        credits = ("# Stock video credits\n\nFree to use under the Pixabay Content License / Pexels License; credit "
                   "is appreciated.\n\n")
    line = (f"- {out}: video by [{c.get('creator') or 'unknown'}]({c.get('creator_url') or c.get('page_url')}) on "
            f"{site.capitalize()}: {c.get('page_url')}\n")
    if line not in credits:
        await sandbox.write(_abs(f"{STOCK_DIR}/CREDITS.md"), credits + line)
    dur = float(r.get("duration_s") or 0)
    times = [round(dur * f, 2) for f in (0.1, 0.35, 0.6, 0.85)]
    sheet = await _sheet(out, f"{STOCK_DIR}/{name}.png", times, [f"{t:.1f}s" for t in times])
    _emit(f"Stock clip {name}: {dur:g}s from {site}", {"path": out, "page": c.get("page_url")})
    return {"path": out, "duration_s": dur, "size": f"{r.get('width')}×{r.get('height')}", "sheet": sheet,
            "credit": line.strip(), "next": f"use it as a shot: {{\"source\": \"clip\", \"path\": \"{out}\", "
                                            "\"in_s\": the moment to start}"}


STOCK_TOOLS = [short_stock, short_stock_pick]
