"""The `trends` toolset: what's working right now in a niche, as data that shorts are built from.

  trend_scan       recent TikToks for the niche's hashtags (Apify), ranked by how far each outperformed its creator's
                   audience (plays ÷ followers): outliers point at formats that work for ordinary accounts
  trend_analyze    a few chosen videos: timed transcript, caption, sound, metrics and a frame sheet, to write a format
                   card from; the downloaded video is deleted once its frames are taken, nothing is republished
  format_save      a format card into the shared library (DB), tagged by niche and platform
  format_search    format cards already learned for a niche

Everything a scan finds is kept in the run folder under video/research/<scan>/. Nothing here knows any product or
niche: the agent brings the hashtags from its niche map.
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import vault
from ..db import FormatCard, select, session, utcnow
from ..policy import SpendDenied, authorize_spend, settle
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from . import apify, media, sandbox
from .shorts import _read_json, _write_json
from .sandbox_tools import _root
from .video import _slugify

TAG = re.compile(r"[a-z0-9_]{2,40}")
MAX_TAGS, MAX_PER_TAG, MAX_ANALYZE = 8, 30, 6
REACH_FLOOR = 100  # followers counted for tiny accounts, so a 50-follower fluke doesn't top the list


def _key() -> str:
    key = vault.get_secret("APIFY_API_TOKEN")
    if not key:
        raise ToolError(apify.NO_KEY)
    return key


def _emit(text: str, data: dict[str, Any]) -> None:
    get_ctx().emit(get_agent_id(), "step", text, data)


def normalize(it: dict[str, Any]) -> dict[str, Any] | None:
    """One TikTok from the Apify actor, in Todd's own shape (the same shape other platforms will use)."""
    if it.get("error") or not it.get("id") or not it.get("playCount"):
        return None
    a, v, m = it.get("authorMeta") or {}, it.get("videoMeta") or {}, it.get("musicMeta") or {}
    subs = [s for s in v.get("subtitleLinks") or [] if apify.stored(s.get("downloadLink"))]
    sub = next((s for s in subs if str(s.get("language", "")).lower().startswith("en")), subs[0] if subs else None)
    return {
        "platform": "tiktok", "id": str(it["id"]), "url": it.get("webVideoUrl"),
        "author": a.get("name"), "followers": int(a.get("fans") or 0),
        "plays": int(it.get("playCount") or 0), "likes": int(it.get("diggCount") or 0),
        "comments": int(it.get("commentCount") or 0), "shares": int(it.get("shareCount") or 0),
        "saves": int(it.get("collectCount") or 0), "duration_s": v.get("duration"),
        "created": it.get("createTimeISO"), "caption": (it.get("text") or "")[:600],
        "hashtags": [h.get("name") for h in it.get("hashtags") or [] if h.get("name")][:20],
        "sound": {"name": m.get("musicName"), "original": bool(m.get("musicOriginal"))},
        "slideshow": bool(it.get("isSlideshow")), "ad": bool(it.get("isAd") or it.get("isSponsored")),
        "subtitles": sub.get("downloadLink") if sub else None,
        "found_by": (it.get("searchHashtag") or {}).get("name") or it.get("input"),
    }


def score(x: dict[str, Any]) -> dict[str, Any]:
    plays = max(x["plays"], 1)
    x["reach"] = round(x["plays"] / max(x["followers"], REACH_FLOOR), 2)  # how far past its own audience it went
    x["engagement"] = round((x["likes"] + x["comments"] + x["shares"] + x["saves"]) / plays, 4)
    x["share_rate"] = round(x["shares"] / plays, 4)  # shares say "send this to a friend": the strongest signal
    return x


def _recent(x: dict[str, Any], days: int) -> bool:
    try:
        made = datetime.fromisoformat(str(x["created"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    return made >= datetime.now(timezone.utc) - timedelta(days=days)


def _brief(x: dict[str, Any]) -> dict[str, Any]:
    return {k: x[k] for k in ("id", "url", "author", "followers", "plays", "reach", "engagement", "share_rate",
                              "duration_s", "created", "sound", "slideshow")} | {
        "caption": x["caption"][:160], "transcript": bool(x["subtitles"])}


@todd_tool(toolset="trends")
async def trend_scan(hashtags: list[str], per_tag: int = 15, days: int = 120, min_plays: int = 10000,
                     keep: int = 12) -> dict:
    """Find what's working in a niche: recent TikToks for its hashtags, ranked by reach (plays ÷ the creator's
    followers), so videos that broke out of a small account come first. Costs about $0.004 a video (Apify); one charge
    per scan. Then pick the strongest few for trend_analyze.

    Args:
        hashtags: 2–8 hashtags people in the niche actually use, without # (e.g. "beerleaguehockey", "buildinpublic")
        per_tag: videos per hashtag (5–30)
        days: only videos from the last this many days
        min_plays: ignore videos with fewer plays
        keep: how many of the best to return (all are saved)
    """
    tags = list(dict.fromkeys(str(t).strip().lstrip("#").lower() for t in hashtags or []))
    if not 1 <= len(tags) <= MAX_TAGS or not all(TAG.fullmatch(t) for t in tags):
        raise ToolError(f"1–{MAX_TAGS} hashtags of letters, digits or _, without #")
    per_tag = max(5, min(int(per_tag), MAX_PER_TAG))
    key = _key()
    ctx = get_ctx()
    est = apify.estimate(apify.TIKTOK, len(tags) * per_tag)
    try:
        entry = await authorize_spend(ctx, amount_usd=max(0.01, math.ceil(est * 100) / 100), merchant="Apify",
                                      description=f"Trend scan: {len(tags) * per_tag} TikToks for #{', #'.join(tags)}",
                                      data={"hashtags": tags, "per_tag": per_tag})
    except SpendDenied as e:
        raise ToolError(str(e)) from e
    _emit(f"Scanning TikTok: #{', #'.join(tags)}", {"hashtags": tags, "per_tag": per_tag})
    try:
        run, items = await apify.run_actor(key, apify.TIKTOK, {
            "hashtags": tags, "resultsPerPage": per_tag, "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES"})
    except Exception as e:
        settle(entry, "failed", {"error": str(e)[:300]})
        raise
    settle(entry, "completed", {"usd": run.get("usageTotalUsd"), "run": run.get("id")})
    found: dict[str, dict[str, Any]] = {}
    for it in items:
        x = normalize(it)
        if x and x["id"] not in found:
            found[x["id"]] = score(x)
    pool = [x for x in found.values() if not x["ad"] and _recent(x, days) and x["plays"] >= min_plays]
    pool.sort(key=lambda x: (x["reach"], x["share_rate"]), reverse=True)
    base = _slugify("-".join(tags))[:40]
    name, n = base, 1
    while (await _read_json(f"video/research/{name}/scan.json")) is not None:
        n += 1
        name = f"{base[:36]}-{n}"
    plays = [x["plays"] for x in pool]
    result = {"scan": name, "hashtags": tags, "days": days, "found": len(found), "kept": len(pool),
              "usd": round(float(run.get("usageTotalUsd") or 0), 4),
              "median_plays": int(statistics.median(plays)) if plays else 0, "videos": pool}
    await _write_json(f"video/research/{name}/scan.json", result)
    _emit(f"Scan {name}: {len(pool)} of {len(found)} videos kept", {"scan": name, "usd": result["usd"]})
    return {"scan": name, "path": f"video/research/{name}/scan.json", "found": len(found), "kept": len(pool),
            "usd": result["usd"], "median_plays": result["median_plays"], "top": [_brief(x) for x in pool[:keep]],
            "next": "trend_analyze the 3–5 strongest (high reach and share rate), then write their format cards"}


def parse_vtt(text: str) -> list[dict[str, Any]]:
    """WebVTT cues: [{start, end, text}]."""
    cues, cur = [], None
    for line in text.splitlines():
        m = re.match(r"(\d+):(\d+):(\d+)\.(\d+)\s+-->\s+(\d+):(\d+):(\d+)\.(\d+)", line.strip())
        if m:
            g = [int(x) for x in m.groups()]
            cur = {"start": g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                   "end": g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000, "text": ""}
            cues.append(cur)
        elif cur is not None and line.strip():
            cur["text"] = f"{cur['text']} {line.strip()}".strip()
    return [c for c in cues if c["text"]]


@todd_tool(toolset="trends")
async def trend_analyze(scan: str, ids: list[str], frames: bool = True) -> dict:
    """Study up to 6 videos from a scan: the timed transcript (TikTok's subtitles, or a free local transcription), the
    caption, the sound, the metrics and, with frames, a contact sheet of 8 moments (video/research/<scan>/<id>.png).
    The downloaded video is deleted once its frames are taken; nothing is republished. Write a format card from what
    you learn (format_save). Frames cost about $0.005 a video.

    Args:
        scan: the scan, from trend_scan
        ids: the video ids to study
        frames: also make the frame sheets (needs the videos)
    """
    data = await _read_json(f"video/research/{scan}/scan.json") if re.fullmatch(r"[a-z0-9-]{1,48}", scan or "") \
        else None
    if data is None:
        raise ToolError(f"no scan {scan!r} in this run: run trend_scan first")
    by_id = {x["id"]: x for x in data["videos"]}
    ids = list(dict.fromkeys(str(i) for i in ids or []))
    if not 1 <= len(ids) <= MAX_ANALYZE or any(i not in by_id for i in ids):
        raise ToolError(f"1–{MAX_ANALYZE} ids from scan {scan}")
    key = _key()
    ctx = get_ctx()
    videos: dict[str, bytes] = {}
    if frames:
        est = apify.estimate(apify.TIKTOK, len(ids), downloads=len(ids))
        try:
            entry = await authorize_spend(ctx, amount_usd=max(0.01, math.ceil(est * 100) / 100), merchant="Apify",
                                          description=f"Download {len(ids)} reference TikToks to study (scan {scan})",
                                          data={"scan": scan, "ids": ids})
        except SpendDenied as e:
            raise ToolError(str(e)) from e
        try:
            run, items = await apify.run_actor(key, apify.TIKTOK, {
                "postURLs": [by_id[i]["url"] for i in ids], "shouldDownloadVideos": True,
                "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES"})
        except Exception as e:
            settle(entry, "failed", {"error": str(e)[:300]})
            raise
        settle(entry, "completed", {"usd": run.get("usageTotalUsd"), "run": run.get("id")})
        for it in items:
            url = next((u for u in it.get("mediaUrls") or [] if apify.stored(u)), None)
            if url and str(it.get("id")) in by_id:
                videos[str(it["id"])] = await apify.fetch(key, url)
    out = []
    for i in ids:
        x = by_id[i]
        cues: list[dict[str, Any]] = []
        if x.get("subtitles"):
            try:
                cues = parse_vtt((await apify.fetch(key, x["subtitles"], max_bytes=500_000)).decode("utf-8", "replace"))
            except ToolError:
                cues = []
        sheet, source = None, "subtitles" if cues else None
        if i in videos:
            path = f"video/research/{scan}/{i}.mp4"
            await media.put(ctx.run_id, path, videos[i])
            if not cues:  # no subtitles: transcribe it here, free
                t = await media.call("/transcribe", {"run_id": ctx.run_id, "src": path}, timeout=300)
                cues = [{"start": w["start"], "end": w["end"], "text": w["word"]} for w in t.get("words") or []]
                source = "local transcription" if cues else None
            f = await media.call("/frames", {"run_id": ctx.run_id, "src": path, "out": f"video/research/{scan}/{i}.png",
                                             "count": 8, "delete_source": True}, timeout=300)
            sheet = f["sheet"]
        study = {**_brief(x), "caption": x["caption"], "hashtags": x["hashtags"], "transcript_source": source,
                 "transcript": [{"t": round(c["start"], 1), "text": c["text"]} for c in cues][:80], "frames": sheet}
        await _write_json(f"video/research/{scan}/{i}.json", study)
        out.append(study)
    _emit(f"Studied {len(out)} videos from {scan}", {"scan": scan, "ids": ids})
    return {"videos": out, "next": "format_save one card per format you see (merge videos that share a format)"}


# ------------------------------------------------------------------------------------------ the format library
def _tags(values: list[str]) -> list[str]:
    return list(dict.fromkeys(t for t in (str(v).strip().lstrip("#").lower() for v in values or []) if t))[:20]


@todd_tool(toolset="trends")
async def format_save(name: str, tags: list[str], hook: dict, beats: list[dict], why: str,
                      examples: list[str], platform: str = "tiktok", length_s: list[float] | None = None,
                      sound: str = "", on_screen: str = "", adapt: str = "") -> dict:
    """Save a format card to the shared library: a reusable shape of video, learned from real examples.

    Args:
        name: what people would call it, e.g. "types of guys at drop-in" or "receipts on screen"
        tags: niche words and hashtags it belongs to (lowercase)
        hook: {"visual": what's on screen in the first 1–2 s, "line": what's said, "text": on-screen text}
        beats: the shape after the hook, in order: [{"what": "…", "seconds": 2}]
        why: why it works, in one or two sentences (the mechanism, not "it's funny")
        examples: video ids from the scans it came from (their metrics are attached)
        platform: "tiktok", "reels" or "shorts"
        length_s: [shortest, longest] of the examples
        sound: original voice, dialogue, a trending sound, music…
        on_screen: caption and text style
        adapt: how to fit a product into it without turning it into an ad
    """
    if not (name or "").strip() or not (why or "").strip():
        raise ToolError("a card needs a name and why it works")
    if not isinstance(hook, dict) or not any(hook.get(k) for k in ("visual", "line", "text")):
        raise ToolError("hook is {visual, line, text}")
    if not isinstance(beats, list) or not beats:
        raise ToolError("beats is a list of {what, seconds}")
    ctx = get_ctx()
    found: dict[str, dict[str, Any]] = {}
    r = await sandbox.call("/files/tree", {"base": f"{_root()}/video/research"}, timeout=30)
    for e in r.get("entries") or []:
        if e["path"].endswith("scan.json"):
            data = await _read_json(f"video/research/{e['path']}")
            for x in (data or {}).get("videos") or []:
                found[x["id"]] = x
    ex = [{k: found[i].get(k) for k in ("id", "url", "author", "followers", "plays", "reach", "share_rate")}
          for i in dict.fromkeys(str(i) for i in examples or []) if i in found]
    if not ex:
        raise ToolError("examples must be video ids from this run's scans")
    card = {"hook": hook, "beats": beats, "why": why.strip(), "length_s": length_s, "sound": sound,
            "on_screen": on_screen, "adapt": adapt}
    with session() as s:
        row = FormatCard(name=name.strip(), platform=platform, tags=_tags(tags), card=card, examples=ex,
                         run_id=ctx.run_id)
        s.add(row)
        s.commit()
        s.refresh(row)
    _emit(f"Format card: {name}", {"id": row.id, "examples": len(ex)})
    return {"id": row.id, "name": row.name, "examples": len(ex)}


@todd_tool(toolset="trends")
async def format_search(tags: list[str], platform: str | None = None, limit: int = 10) -> dict:
    """Format cards already learned for a niche (any run), best matches and newest first.

    Args:
        tags: niche words and hashtags
        platform: only this platform
        limit: at most this many
    """
    want = set(_tags(tags))
    with session() as s:
        q = select(FormatCard)
        if platform:
            q = q.where(FormatCard.platform == platform)
        rows = s.exec(q.order_by(FormatCard.created_at.desc()).limit(500)).all()  # type: ignore[attr-defined]
    hits = sorted(((len(want & set(r.tags)), r) for r in rows if want & set(r.tags)),
                  key=lambda h: (h[0], h[1].created_at), reverse=True)
    age = lambda r: (utcnow() - r.created_at.replace(tzinfo=r.created_at.tzinfo or timezone.utc)).days  # noqa: E731
    return {"cards": [{"id": r.id, "name": r.name, "platform": r.platform, "tags": r.tags, **r.card,
                       "examples": r.examples, "age_days": age(r)} for _, r in hits[:max(1, min(limit, 30))]]}


TRENDS_TOOLS = [trend_scan, trend_analyze, format_save, format_search]
