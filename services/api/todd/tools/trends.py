"""The `trends` toolset: what's working right now in a niche, as data that shorts are built from.

  trend_scan       recent TikToks for what the niche searches and the hashtags it uses (Apify), ranked by how far each
                   outperformed its creator's audience (plays ÷ followers): outliers point at formats that work for
                   ordinary accounts
  trend_analyze    a few chosen videos, decoded shot by shot: where each cut is, a frame from every shot (shown to the
                   agent), the words said over each shot, caption, sound and metrics, to write a format card from; the
                   downloaded video is deleted once its frames are taken, nothing is republished
  format_save      a format card into the shared library (DB), tagged by niche and platform
  format_search    format cards already learned for a niche

Everything a scan finds is kept in the run folder under video/research/<scan>/. Nothing here knows any product or
niche: the agent brings the search phrases and hashtags from its niche map.
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import FormatCard, select, session, utcnow
from ..policy import SpendDenied, authorize_spend, settle
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from . import apify, media, sandbox
from .human import secret_or_ask
from .shorts import _read_json, _write_json
from .sandbox_tools import _root
from .video import _slugify

TAG = re.compile(r"[a-z0-9_]{2,40}")
QUERY = re.compile(r"[\w][\w '&.+-]{1,58}[\w.]")
MAX_TAGS, MAX_QUERIES, MAX_PER_TAG, MAX_ANALYZE = 8, 6, 30, 6
SHEETS_PER_VIDEO = 2  # shot sheets shown per studied video (30 frames): six videos stay within one result's images
REACH_FLOOR = 100  # followers counted for tiny accounts, so a 50-follower fluke doesn't top the list


async def _key() -> str:
    return await secret_or_ask("APIFY_API_TOKEN", apify.ASK_KEY)


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
        "found_by": (f"#{(it.get('searchHashtag') or {}).get('name')}" if (it.get("searchHashtag") or {}).get("name")
                     else it.get("searchQuery") or it.get("input") or ("related" if it.get("isRelated") else None)),
        "language": (it.get("textLanguage") or "").lower() or None,
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
    return {k: x.get(k) for k in ("id", "url", "author", "followers", "plays", "reach", "engagement", "share_rate",
                                  "duration_s", "created", "sound", "slideshow", "found_by")} | {
        "caption": x["caption"][:160], "transcript": bool(x["subtitles"])}


@todd_tool(toolset="trends")
async def trend_scan(queries: list[str] | None = None, hashtags: list[str] | None = None,
                     related: list[str] | None = None, per_tag: int = 15, days: int = 120, min_plays: int = 10000,
                     keep: int = 12, language: str = "en") -> dict:
    """Find what's working in a niche: recent TikToks for what its people search and the hashtags they use, ranked by
    reach (plays ÷ the creator's followers), so videos that broke out of a small account come first. Search phrases
    find the niche more precisely than hashtags (a hashtag can mean something else in another country or sport); use
    both. Costs about $0.004 a video (Apify); one charge per scan. Then pick the strongest few for trend_analyze.

    Args:
        queries: 1–6 phrases people in the niche would type into TikTok search, about the situation rather than the
            product (e.g. "drop in hockey", "beer league goalie", "vibe coding app")
        hashtags: up to 8 hashtags people in the niche actually use, without # (e.g. "beerleaguehockey")
        related: ids of up to 3 on-niche videos from an earlier scan in this run: TikTok's "related videos" for
            each (a second pass that stays in the niche better than any tag)
        per_tag: videos per phrase, hashtag or related video (5–30)
        days: only videos from the last this many days
        min_plays: ignore videos with fewer plays
        keep: how many of the best to return (all are saved)
        language: keep only captions in this language (TikTok's guess, e.g. "en"; "" keeps every language)
    """
    tags = list(dict.fromkeys(str(t).strip().lstrip("#").lower() for t in hashtags or []))
    qs = list(dict.fromkeys(" ".join(str(q).split()).lower() for q in queries or []))
    rel = list(dict.fromkeys(str(i) for i in related or []))
    if not tags and not qs and not rel:
        raise ToolError("give search phrases (queries), hashtags and/or related video ids")
    if len(rel) > 3:
        raise ToolError("up to 3 related videos")
    known: dict[str, str] = {}
    if rel:
        r = await sandbox.call("/files/tree", {"base": f"{_root()}/video/research"}, timeout=30)
        for e in r.get("entries") or []:
            if e["path"].endswith("scan.json"):
                for x in ((await _read_json(f"video/research/{e['path']}")) or {}).get("videos") or []:
                    if x.get("url"):
                        known[x["id"]] = x["url"]
        if any(i not in known for i in rel):
            raise ToolError("related takes ids of videos from this run's scans")
    if len(tags) > MAX_TAGS or not all(TAG.fullmatch(t) for t in tags):
        raise ToolError(f"up to {MAX_TAGS} hashtags of letters, digits or _, without #")
    if len(qs) > MAX_QUERIES or not all(QUERY.fullmatch(q) for q in qs):
        raise ToolError(f"up to {MAX_QUERIES} search phrases of 3–60 letters, digits and spaces")
    per_tag = max(5, min(int(per_tag), MAX_PER_TAG))
    key = await _key()
    ctx = get_ctx()
    est = apify.estimate(apify.TIKTOK, (len(tags) + len(qs) + len(rel) * 2) * per_tag)
    usd = max(0.01, math.ceil(est * 100) / 100)
    looked = [f'"{q}"' for q in qs] + [f"#{t}" for t in tags] + [f"videos like {i}" for i in rel]
    try:
        entry = await authorize_spend(ctx, amount_usd=usd, merchant="Apify",
                                      description=f"Trend scan: about {(len(tags) + len(qs) + len(rel)) * per_tag} "
                                                  f"TikToks for {', '.join(looked)}",
                                      data={"queries": qs, "hashtags": tags, "related": rel, "per_tag": per_tag})
    except SpendDenied as e:
        raise ToolError(str(e)) from e
    _emit(f"Scanning TikTok: {', '.join(looked)}", {"queries": qs, "hashtags": tags, "per_tag": per_tag})
    payload: dict[str, Any] = {"resultsPerPage": per_tag, "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES"}
    if qs:
        payload |= {"searchQueries": qs, "searchSection": "/video"}
    if tags:
        payload["hashtags"] = tags
    if rel:
        payload |= {"postURLs": [known[i] for i in rel], "scrapeRelatedVideos": True}
    try:
        run, items = await apify.run_actor(key, apify.TIKTOK, payload, max_charge_usd=usd)
    except Exception as e:
        settle(entry, "failed", {"error": str(e)[:300]})
        raise
    settle(entry, "completed", {"usd": run.get("usageTotalUsd"), "run": run.get("id")})
    found: dict[str, dict[str, Any]] = {}
    for it in items:
        x = normalize(it)
        if x and x["id"] not in found:
            found[x["id"]] = score(x)
    lang = (language or "").strip().lower()
    other = {x["id"] for x in found.values() if lang and x.get("language") not in (None, "un", lang)}
    pool = [x for x in found.values() if not x["ad"] and _recent(x, days) and x["plays"] >= min_plays
            and x["id"] not in other and x["id"] not in rel]
    pool.sort(key=lambda x: (x["reach"], x["share_rate"]), reverse=True)
    base = _slugify("-".join([*qs, *tags]) or f"like-{'-'.join(rel)}")[:40]
    name, n = base, 1
    while (await _read_json(f"video/research/{name}/scan.json")) is not None:
        n += 1
        name = f"{base[:36]}-{n}"
    plays = [x["plays"] for x in pool]
    searched = {q: sum(1 for x in found.values() if x.get("found_by") == q) for q in qs}
    result = {"scan": name, "queries": qs, "hashtags": tags, "related": rel, "days": days, "found": len(found),
              "other_language": len(other), "kept": len(pool),
              "usd": round(float(run.get("usageTotalUsd") or 0), 4),
              "median_plays": int(statistics.median(plays)) if plays else 0, "videos": pool}
    await _write_json(f"video/research/{name}/scan.json", result)
    _emit(f"Scan {name}: {len(pool)} of {len(found)} videos kept", {"scan": name, "usd": result["usd"]})
    out = {"scan": name, "path": f"video/research/{name}/scan.json", "found": len(found), "kept": len(pool),
           "usd": result["usd"], "median_plays": result["median_plays"], "top": [_brief(x) for x in pool[:keep]],
           "next": "trend_analyze the 3–5 strongest that are really in this niche (high reach and share rate; read "
                   "the captions: skip off-niche, ads and big accounts), then write their format cards"}
    empty = [q for q, k in searched.items() if not k]
    if empty:
        out["note"] = (f"search found nothing for {', '.join(empty)} (TikTok search is sometimes refused): try other "
                       "phrases, or lean on hashtags")
    return out


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


def by_shot(shots: list[dict[str, Any]], cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The words said over each shot: a cue (a subtitle line, or a word) goes to the shot it starts in."""
    out = [{**sh, "said": ""} for sh in shots]
    for c in cues:
        k = next((i for i, sh in enumerate(out) if sh["start"] <= c["start"] < sh["end"]), len(out) - 1)
        if out:
            out[k]["said"] = f"{out[k]['said']} {c['text']}".strip()
    return out


def speech_share(cues: list[dict[str, Any]], total: float) -> float:
    """How much of the video has someone talking (0–1): near 1 is voice-led, near 0 is text and sound."""
    if not total or not cues:
        return 0.0
    spans = sorted((max(0.0, c["start"]), min(total, max(c["end"], c["start"]))) for c in cues)
    covered, cur_a, cur_b = 0.0, None, None
    for a, b in spans:
        if cur_b is None or a > cur_b:
            covered += (cur_b - cur_a) if cur_b is not None else 0.0
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    covered += (cur_b - cur_a) if cur_b is not None else 0.0
    return round(min(1.0, covered / total), 2)


@todd_tool(toolset="trends")
async def trend_analyze(scan: str, ids: list[str], frames: bool = True) -> dict:
    """Study up to 6 videos from a scan, shot by shot. For each: where every cut is, a sheet with a frame from every
    shot (shown to you with this result, in the order of the videos: #1 is the first shot, "later" is the end of a long
    one), the words said over each shot (TikTok's subtitles, or a free local transcription), how much of it is speech,
    the caption, the sound and the metrics. Look at every sheet before writing a card: what each shot actually shows,
    who or what is on camera, how it's filmed, the exact on-screen text and where it sits. The downloaded video is
    deleted once its frames are taken; nothing is republished. Frames cost about $0.005 a video.

    Args:
        scan: the scan, from trend_scan
        ids: the video ids to study
        frames: also decode the shots (needs the videos; without it you only get words and numbers)
    """
    data = await _read_json(f"video/research/{scan}/scan.json") if re.fullmatch(r"[a-z0-9-]{1,48}", scan or "") \
        else None
    if data is None:
        raise ToolError(f"no scan {scan!r} in this run: run trend_scan first")
    by_id = {x["id"]: x for x in data["videos"]}
    ids = list(dict.fromkeys(str(i) for i in ids or []))
    if not 1 <= len(ids) <= MAX_ANALYZE or any(i not in by_id for i in ids):
        raise ToolError(f"1–{MAX_ANALYZE} ids from scan {scan}")
    key = await _key()
    ctx = get_ctx()
    videos: dict[str, bytes] = {}
    if frames:
        est = apify.estimate(apify.TIKTOK, len(ids), downloads=len(ids))
        usd = max(0.01, math.ceil(est * 100) / 100)
        try:
            entry = await authorize_spend(ctx, amount_usd=usd, merchant="Apify",
                                          description=f"Download {len(ids)} reference TikToks to study (scan {scan})",
                                          data={"scan": scan, "ids": ids})
        except SpendDenied as e:
            raise ToolError(str(e)) from e
        try:
            run, items = await apify.run_actor(key, apify.TIKTOK, {
                "postURLs": [by_id[i]["url"] for i in ids], "shouldDownloadVideos": True,
                "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES"}, max_charge_usd=usd)
        except Exception as e:
            settle(entry, "failed", {"error": str(e)[:300]})
            raise
        settle(entry, "completed", {"usd": run.get("usageTotalUsd"), "run": run.get("id")})
        for it in items:
            url = next((u for u in it.get("mediaUrls") or [] if apify.stored(u)), None)
            if url and str(it.get("id")) in by_id:
                videos[str(it["id"])] = await apify.fetch(key, url)
    out = []
    shown = 0
    for i in ids:
        x = by_id[i]
        cues: list[dict[str, Any]] = []
        if x.get("subtitles"):
            try:
                cues = parse_vtt((await apify.fetch(key, x["subtitles"], max_bytes=500_000)).decode("utf-8", "replace"))
            except ToolError:
                cues = []
        sheets, source, cut = [], "subtitles" if cues else None, None
        if i in videos:
            path = f"video/research/{scan}/{i}.mp4"
            await media.put(ctx.run_id, path, videos[i])
            if not cues:  # no subtitles: transcribe it here, free
                t = await media.call("/transcribe", {"run_id": ctx.run_id, "src": path}, timeout=300)
                cues = [{"start": w["start"], "end": w["end"], "text": w["word"]} for w in t.get("words") or []]
                source = "local transcription" if cues else None
            cut = await media.call("/shots", {"run_id": ctx.run_id, "src": path, "out": f"video/research/{scan}/{i}.jpg",
                                              "inline": True, "delete_source": True}, timeout=300)
            sheets = cut["sheets"]
            for b64 in (cut.get("images_b64") or [])[:SHEETS_PER_VIDEO]:
                get_ctx().push_image(get_agent_id(), b64, "image/jpeg")
                shown += 1
        total = float((cut or {}).get("duration_s") or x.get("duration_s") or 0)
        study = {**_brief(x), "caption": x["caption"], "hashtags": x["hashtags"], "transcript_source": source,
                 "speech_share": speech_share(cues, total),
                 "transcript": [{"t": round(c["start"], 1), "text": c["text"]} for c in cues][:80]}
        if cut:
            study |= {"duration_s": cut["duration_s"], "cuts": cut["cuts"], "avg_shot_s": cut["avg_shot_s"], "longest_shot_s": cut["longest_shot_s"],
                      "shots": by_shot(cut["shots"], cues)[:45], "sheets": sheets}
            study.pop("transcript")  # the words are in the shots
        await _write_json(f"video/research/{scan}/{i}.json", study)
        out.append(study)
    _emit(f"Studied {len(out)} videos from {scan}", {"scan": scan, "ids": ids})
    return {"videos": out, "sheets_shown": shown,
            "next": "look at each sheet next to its shot list, then format_save one card per format you see (merge "
                    "videos that share one): its shot list as it really is (what each shot shows, how it's filmed, the "
                    "on-screen text), the audio, the text style and the pace"}


# ------------------------------------------------------------------------------------------ the format library
def _tags(values: list[str]) -> list[str]:
    return list(dict.fromkeys(t for t in (str(v).strip().lstrip("#").lower() for v in values or []) if t))[:20]


# What a shot shows, as a fixed list, so a card can be produced from any product's tools (see the shorts guide)
SHOT_TYPES = {
    "screen": "an app or website on a phone or computer screen",
    "screenshot": "a still of a post, comment, chat, review or receipt, often with someone in front of it",
    "talking-head": "someone talking to the camera (selfie, face cam, green screen)",
    "person": "someone doing something or reacting, not talking to the camera",
    "pov-hands": "first-person view: hands doing something",
    "place": "a place or scene with nobody in focus (b-roll)",
    "object": "a close-up of a thing",
    "text-only": "text on a plain or blurred background",
    "reused": "a clip from TV, a game, a film or another creator",
    "other": "anything else (say what in `shows`)",
}
AUDIO_MODES = {
    "voiceover": "a voice over footage, nobody talking on camera",
    "to-camera": "someone talks to the camera",
    "skit": "people talk to each other (acted)",
    "text+sound": "nobody talks: on-screen text over a sound or song",
    "natural": "the real sound of the moment (a crowd, a rink, taps and clicks)",
}


async def _studied(ids: list[str]) -> dict[str, dict[str, Any]]:
    """The shot-by-shot studies (trend_analyze) of these videos, from any scan in this run."""
    out: dict[str, dict[str, Any]] = {}
    r = await sandbox.call("/files/tree", {"base": f"{_root()}/video/research"}, timeout=30)
    for e in r.get("entries") or []:
        name = e["path"].rsplit("/", 1)[-1]
        if name.endswith(".json") and name[:-5] in ids and name[:-5] not in out:
            data = await _read_json(f"video/research/{e['path']}")
            if data and data.get("cuts") is not None:
                out[name[:-5]] = data
    return out


def measured(studies: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The examples' pace, measured (not judged): shots, average shot length, length, how much is speech."""
    if not studies:
        return None
    avg = lambda k: round(statistics.mean(float(x[k]) for x in studies if x.get(k) is not None), 2)  # noqa: E731
    lengths = [float(x.get("duration_s") or 0) for x in studies if x.get("duration_s")]
    return {"examples": len(studies), "shots": round(statistics.mean(x["cuts"] + 1 for x in studies), 1),
            "avg_shot_s": avg("avg_shot_s"), "speech_share": avg("speech_share"),
            "length_s": [round(min(lengths), 1), round(max(lengths), 1)] if lengths else None}


def _card_shots(shots: Any) -> list[dict[str, Any]]:
    if not isinstance(shots, list) or not 1 <= len(shots) <= 40:
        raise ToolError("shots is the shot list as it really is: 1–40 of {seconds, type, shows, camera, text, said}")
    out = []
    for i, sh in enumerate(shots, 1):
        if not isinstance(sh, dict) or sh.get("type") not in SHOT_TYPES or not str(sh.get("shows") or "").strip():
            raise ToolError(f"shot {i}: {{\"type\": one of {', '.join(SHOT_TYPES)}, \"shows\": what's on screen, "
                            "\"seconds\", \"camera\", \"text\", \"said\"}")
        out.append({"seconds": round(float(sh.get("seconds") or 0), 2), "type": sh["type"],
                    **{k: " ".join(str(sh.get(k) or "").split())[:300] for k in ("shows", "camera", "text", "said")
                       if sh.get(k)}})
    return out


@todd_tool(toolset="trends")
async def format_save(name: str, tags: list[str], hook: dict, shots: list[dict], audio: str, why: str,
                      examples: list[str], platform: str = "tiktok", length_s: list[float] | None = None,
                      sound: str = "", text_style: str = "", adapt: str = "", beats: list[dict] | None = None) -> dict:
    """Save a format card to the shared library: a reusable shape of video, learned from real examples you studied
    with trend_analyze. Write what you saw, then what it means: the shot list as it really is (from the sheets), then
    why it works and how to adapt it. The examples' measured pace (shots, average shot length, speech share) is
    attached by Todd from the studies.

    Args:
        name: what people would call it, e.g. "types of guys at drop-in" or "receipts on screen"
        tags: niche words and hashtags it belongs to (lowercase)
        hook: the first 1–2 s: {"visual": what's on screen, "line": what's said ("" if nothing), "text": on-screen text}
        shots: the shot list of the best example, in order (group a fast montage into one entry): {"seconds": how
            long, "type": screen | screenshot | talking-head | person | pov-hands | place | object | text-only | reused
            | other, "shows": what's on screen (who, doing what, where), "camera": how it's filmed (selfie, handheld,
            propped, screen recording, punch-in…), "text": the on-screen text, word for word, "said": what's said}
        audio: voiceover | to-camera | skit | text+sound | natural
        why: why it works, in one or two sentences, pointing at shots (e.g. "shot 1 shows the pain before any product")
        examples: video ids from the scans it came from (their metrics and measured pace are attached)
        platform: "tiktok", "reels" or "shorts"
        length_s: [shortest, longest] of the examples
        sound: the sound or song, and whether it's original
        text_style: how the text looks and where it sits (e.g. "one lowercase line in a white box, top third, the
            whole video"; "word-by-word captions, center")
        adapt: how a product fits in without turning it into an ad: what to keep (the mechanism), what to change
        beats: optional, the story in plain steps if the shot list doesn't make it obvious: [{"what", "seconds"}]
    """
    if not (name or "").strip() or not (why or "").strip():
        raise ToolError("a card needs a name and why it works")
    if not isinstance(hook, dict) or not any(hook.get(k) for k in ("visual", "line", "text")):
        raise ToolError("hook is {visual, line, text}")
    if audio not in AUDIO_MODES:
        raise ToolError(f"audio is one of: {'; '.join(f'{k} ({v})' for k, v in AUDIO_MODES.items())}")
    card_shots = _card_shots(shots)
    ctx = get_ctx()
    found: dict[str, dict[str, Any]] = {}
    r = await sandbox.call("/files/tree", {"base": f"{_root()}/video/research"}, timeout=30)
    for e in r.get("entries") or []:
        if e["path"].endswith("scan.json"):
            data = await _read_json(f"video/research/{e['path']}")
            for x in (data or {}).get("videos") or []:
                found[x["id"]] = x
    ids = [i for i in dict.fromkeys(str(i) for i in examples or []) if i in found]
    ex = [{k: found[i].get(k) for k in ("id", "url", "author", "followers", "plays", "reach", "share_rate")}
          for i in ids]
    if not ex:
        raise ToolError("examples must be video ids from this run's scans")
    studies = await _studied(ids)
    card = {"hook": hook, "shots": card_shots, "audio": audio, "why": why.strip(), "length_s": length_s,
            "sound": sound, "text_style": text_style, "adapt": adapt, "measured": measured(list(studies.values())),
            "needs": sorted({sh["type"] for sh in card_shots})}
    if beats:
        card["beats"] = beats
    with session() as s:
        row = FormatCard(name=name.strip(), platform=platform, tags=_tags(tags), card=card, examples=ex,
                         run_id=ctx.run_id)
        s.add(row)
        s.commit()
        s.refresh(row)
    _emit(f"Format card: {name}", {"id": row.id, "examples": len(ex)})
    out = {"id": row.id, "name": row.name, "examples": len(ex), "measured": card["measured"], "needs": card["needs"]}
    if not studies:
        out["note"] = "none of the examples was studied shot by shot (trend_analyze with frames): no measured pace"
    return out


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
    cards = [{"id": r.id, "name": r.name, "platform": r.platform, "tags": r.tags, **r.card, "examples": r.examples,
              "age_days": age(r), "decoded": bool(r.card.get("shots"))} for _, r in hits[:max(1, min(limit, 30))]]
    out: dict[str, Any] = {"cards": cards}
    if any(not c["decoded"] for c in cards):
        out["note"] = ("cards with decoded=false were written before videos were studied shot by shot: they have no "
                       "shot list, audio mode or measured pace. Scan again and save fresh cards rather than producing "
                       "from those.")
    return out


TRENDS_TOOLS = [trend_scan, trend_analyze, format_save, format_search]
