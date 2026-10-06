"""The `video` toolset: short vertical slideshows (9:16) from stock photos (Pexels) and the run's own images.

The storyboard is the state. It lives in the run's folder (video/<slug>/storyboard.json), so it shows in the Files
view and survives restarts:
  video_new         write the storyboard: 3–12 shots, each a visual description and a caption
  video_find_shots  search images for one shot by meaning, ranked with the shots already picked in view
  video_pick        use one candidate for a shot (its file is copied to video/<slug>/assets/)
  video_render      captioned 1080×1920 slides (video/<slug>/slides/NN.png), the MP4 (video/<slug>/<slug>.mp4), a
                    contact sheet and CREDITS.md

Ranking (the "story state"), for an image vector v:
  score = cos(v, text) + STYLE_WEIGHT·cos(v, mean of the picked images) + CREATOR_BONUS (same photographer as a pick)
Picked images and near-duplicates of them are left out. Embeddings come from the media service and are cached in
MediaAsset, so a stock photo is downloaded and embedded once, whichever run finds it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import posixpath
import re
import shlex
from typing import Any

import httpx
from sqlalchemy.exc import IntegrityError

from .. import media_index, vault
from ..db import MediaAsset, select, session
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from . import media, sandbox
from .sandbox_tools import _abs, _root

STYLE_WEIGHT = 0.35  # how much matching the look of the picked shots counts next to matching the shot's text
CREATOR_BONUS = 0.03  # same photographer as a picked shot (a stand-in for "same shoot")
NEAR_DUPLICATE = 0.95  # cosine above this to a picked image: the same picture, left out

ASPECTS = {"9:16": (1080, 1920)}
POSITIONS = ("top", "middle", "bottom")
MIN_SHOTS, MAX_SHOTS = 3, 12
MIN_DURATION, MAX_DURATION, DEFAULT_DURATION = 1.5, 6.0, 2.5
MAX_CAPTION = 90
SOURCES = ("pexels", "workspace")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")
MOTIONS = ("kenburns", "none")
FPS = 30
LIBRARY_DIR = "video/library"  # the run's own images (app screenshots, product shots), searched on every call
LIBRARY_MAX = 60
CACHE_DIR = "video/_cache"  # downloaded stock photos, as <sha256>.<ext>
MAX_CANDIDATES = 24  # remembered per shot, across searches, so any of them can be picked
PEXELS_SEARCH = "https://api.pexels.com/v1/search"
PEXELS_PER_PAGE = 15
MIN_WIDTH = 1080
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
SHOT_ID = re.compile(r"s[0-9]{1,2}")
NO_PEXELS = ("PEXELS_API_KEY isn't in the vault. Get a free key at https://www.pexels.com/api/ and ask the human "
             "for it with ask_human(..., secret_name=\"PEXELS_API_KEY\"), or search only the run's own images with "
             "sources=[\"workspace\"].")

_locks: dict[str, asyncio.Lock] = {}


# ------------------------------------------------------------------------------------------ storyboard
def _board_path(slug: str) -> str:
    return f"video/{slug}/storyboard.json"


def _lock(slug: str) -> asyncio.Lock:
    """One writer per storyboard at a time (an agent's tool calls in one turn run concurrently)."""
    return _locks.setdefault(f"{get_ctx().run_id}/{slug}", asyncio.Lock())


async def _load(slug: str) -> dict[str, Any]:
    if not SLUG.fullmatch(slug or ""):
        raise ToolError(f"bad slug {slug!r}: use the one video_new returned")
    try:
        r = await sandbox.call("/files/view", {"base": _root(), "path": _board_path(slug), "raw": True}, timeout=30)
    except ToolError as e:
        if "-> 404" in str(e):
            raise ToolError(f"no storyboard {slug!r} in this run: start one with video_new") from e
        raise
    return json.loads(base64.b64decode(r["data"]))


async def _save(board: dict[str, Any]) -> None:
    await sandbox.write(_abs(_board_path(board["slug"])), json.dumps(board, indent=2, ensure_ascii=False) + "\n")


def _shot(board: dict[str, Any], shot_id: str) -> dict[str, Any]:
    if not SHOT_ID.fullmatch(shot_id or ""):  # it becomes part of a file name
        raise ToolError(f"bad shot_id {shot_id!r}: shots are s1, s2, …")
    for s in board["shots"]:
        if s["id"] == shot_id:
            return s
    raise ToolError(f"no shot {shot_id!r} in {board['slug']}: shots are {', '.join(s['id'] for s in board['shots'])}")


def _state(board: dict[str, Any]) -> str:
    picked = sum(1 for s in board["shots"] if s.get("pick"))
    return f"{picked} of {len(board['shots'])} shots picked"


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return s or "video"


async def _taken(slug: str) -> bool:
    try:
        await _load(slug)
        return True
    except ToolError as e:
        if "no storyboard" in str(e):
            return False
        raise


def _emit(text: str, data: dict[str, Any]) -> None:
    get_ctx().emit(get_agent_id(), "step", text, data)


@todd_tool(toolset="video")
async def video_new(title: str, shots: list[dict], style_note: str = "", aspect: str = "9:16") -> dict:
    """Start a slideshow: writes its storyboard to video/<slug>/storyboard.json in the run folder.

    Args:
        title: what the video is, e.g. "dropin.hockey launch"
        shots: 3–12 slides in order, each {"text": what the image shows, "caption": on-screen text (max 90 chars),
            "caption_position": "top" | "middle" | "bottom" (default "middle"), "duration_s": 1.5–6 (default 2.5)}
        style_note: the look every image should share, e.g. "bright, natural light, minimal"; added to each search
        aspect: only "9:16" for now
    """
    if aspect not in ASPECTS:
        raise ToolError(f"aspect {aspect!r} isn't supported yet: use \"9:16\"")
    if not (title or "").strip():
        raise ToolError("give the video a title")
    if not isinstance(shots, list) or not MIN_SHOTS <= len(shots) <= MAX_SHOTS:
        got = len(shots) if isinstance(shots, list) else 0
        raise ToolError(f"a slideshow has {MIN_SHOTS}–{MAX_SHOTS} shots (got {got})")
    out = []
    for i, s in enumerate(shots, 1):
        if not isinstance(s, dict) or not str(s.get("text") or "").strip():
            raise ToolError(f"shot {i} needs a \"text\" describing what the image shows")
        caption = " ".join(str(s.get("caption") or "").split())
        if len(caption) > MAX_CAPTION:
            raise ToolError(f"shot {i}'s caption is {len(caption)} characters; keep it to {MAX_CAPTION}")
        position = s.get("caption_position") or "middle"
        if position not in POSITIONS:
            raise ToolError(f"shot {i}: caption_position is one of {', '.join(POSITIONS)}")
        try:
            duration = float(s.get("duration_s") or DEFAULT_DURATION)
        except (TypeError, ValueError) as e:
            raise ToolError(f"shot {i}: duration_s must be a number of seconds") from e
        if not MIN_DURATION <= duration <= MAX_DURATION:
            raise ToolError(f"shot {i}: duration_s must be {MIN_DURATION}–{MAX_DURATION:g} seconds")
        out.append({"id": f"s{i}", "text": str(s["text"]).strip(), "caption": caption, "caption_position": position,
                    "duration_s": duration, "status": "open", "candidates": [], "scores": {}, "pick": None})
    base = _slugify(title)
    slug, n = base, 1
    while await _taken(slug):
        n += 1
        slug = f"{base[:44]}-{n}"
    width, height = ASPECTS[aspect]
    board = {"version": 1, "slug": slug, "title": title.strip(), "aspect": aspect, "width": width, "height": height,
             "style_note": " ".join(style_note.split()), "shots": out}
    await _save(board)
    _emit(f"Storyboard {slug}: {len(out)} shots", {"slug": slug, "path": _board_path(slug)})
    return {"slug": slug, "path": _board_path(slug),
            "shots": [{"id": s["id"], "text": s["text"], "caption": s["caption"]} for s in out],
            "next": f"video_find_shots(\"{slug}\", \"s1\"), then video_pick; shot by shot, in order."}


# ------------------------------------------------------------------------------------------ search
async def pexels_search(key: str, query: str, per_page: int = PEXELS_PER_PAGE) -> list[dict[str, Any]]:
    """Portrait photos from Pexels' search API."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(PEXELS_SEARCH, params={"query": query, "orientation": "portrait", "per_page": per_page},
                            headers={"Authorization": key})
    except httpx.HTTPError as e:
        raise ToolError(f"Pexels unreachable: {e}") from e
    if r.status_code in (401, 403):
        raise ToolError("Pexels refused PEXELS_API_KEY: ask the human to check it in Settings → Vault")
    if r.status_code == 429:
        raise ToolError("Pexels' hourly limit is used up: try later, or use sources=[\"workspace\"]")
    if r.status_code >= 400:
        raise ToolError(f"Pexels search -> {r.status_code}: {r.text[:300]}")
    return list(r.json().get("photos") or [])


def _slide_src(photo: dict[str, Any]) -> str:
    """The photo at slide size: 1920 px tall (Pexels' large2x is only 1300 px tall for a portrait photo)."""
    src = photo.get("src") or {}
    original = str(src.get("original") or "")
    if original.startswith("https://images.pexels.com/"):
        return original.split("?")[0] + "?auto=compress&cs=tinysrgb&h=1920"
    return str(src.get("large2x") or original)


def _store(asset: MediaAsset) -> MediaAsset:
    """Insert a new asset; if another search stored the same one first, use that."""
    with session() as s:
        s.add(asset)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            found = s.exec(select(MediaAsset).where(MediaAsset.source == asset.source,
                                                    MediaAsset.source_id == asset.source_id,
                                                    MediaAsset.model == asset.model)).first()
            if found:
                return found
            raise
    media_index.store_vec(asset.id, asset.embedding)
    return asset


def _existing(source: str, source_ids: list[str], model: str) -> dict[str, MediaAsset]:
    if not source_ids:
        return {}
    q = select(MediaAsset).where(MediaAsset.source == source, MediaAsset.model == model,
                                 MediaAsset.source_id.in_(source_ids))  # type: ignore[attr-defined]
    with session() as s:
        rows = s.exec(q).all()
    return {a.source_id: a for a in rows}


async def _pexels(run_id: str, key: str, query: str, model: str) -> tuple[list[MediaAsset], list[str]]:
    photos = [p for p in await pexels_search(key, query) if int(p.get("width") or 0) >= MIN_WIDTH and p.get("id")]
    have = _existing("pexels", [str(p["id"]) for p in photos], model)
    new = [p for p in photos if str(p["id"]) not in have]
    problems: list[str] = []
    if new:
        r = await media.embed_images(run_id, [{"url": _slide_src(p)} for p in new], CACHE_DIR)
        for p, res in zip(new, r["results"]):
            if not res.get("ok") or not res.get("vector"):
                problems.append(f"pexels {p['id']}: {res.get('error') or 'no vector'}")
                continue
            have[str(p["id"])] = _store(MediaAsset(
                source="pexels", source_id=str(p["id"]), run_id=run_id, url=_slide_src(p), sha256=res["sha256"],
                creator_id=str(p.get("photographer_id") or "") or None, creator_name=p.get("photographer"),
                creator_url=p.get("photographer_url"), width=res.get("width") or 0, height=res.get("height") or 0,
                alt=p.get("alt") or None, license="pexels", model=r.get("model") or model, embedding=res["vector"]))
    return [have[str(p["id"])] for p in photos if str(p["id"]) in have], problems


def _rel(path: str, ext: tuple[str, ...] = IMAGE_EXT, what: str = "images") -> str:
    """An image (or audio) path from the agent, relative to the run folder; refused if it points outside it."""
    root = _root()
    try:
        p = _abs(path)
    except ToolError as e:
        raise ToolError(f"{path}: outside this run's folder") from e
    if p == root:
        raise ToolError(f"{path}: not a file")
    rel = p[len(root) + 1:]
    if not rel.lower().endswith(ext):
        raise ToolError(f"{path}: only {'/'.join(ext)} {what}")
    return rel


async def _library() -> list[str]:
    r = await sandbox.call("/files/tree", {"base": f"{_root()}/{LIBRARY_DIR}"}, timeout=30)
    files = [e["path"] for e in r.get("entries") or []
             if not e.get("dir") and not e.get("link") and e["path"].lower().endswith(IMAGE_EXT)]
    return [f"{LIBRARY_DIR}/{p}" for p in sorted(files)[:LIBRARY_MAX]]


async def _workspace(run_id: str, paths: list[str], model: str) -> tuple[set[str], list[str]]:
    """Embed the run's images that are new or changed since they were last embedded. Returns (source_ids of the ones
    usable now, problems)."""
    ids = {p: f"{run_id}/{p}" for p in paths}
    have = _existing("workspace", list(ids.values()), model)
    usable: set[str] = set()
    problems: list[str] = []
    for i in range(0, len(paths), 40):
        batch = paths[i:i + 40]
        items = [{"path": p, "known_sha256": have[ids[p]].sha256 if ids[p] in have else None} for p in batch]
        r = await media.embed_images(run_id, items, CACHE_DIR)
        for p, res in zip(batch, r["results"]):
            if not res.get("ok"):
                problems.append(f"{p}: {res.get('error')}")
                continue
            usable.add(ids[p])
            if res.get("unchanged"):
                continue
            fields = dict(sha256=res["sha256"], width=res.get("width") or 0, height=res.get("height") or 0,
                          embedding=res["vector"])
            if ids[p] in have:
                with session() as s:
                    a = s.get(MediaAsset, have[ids[p]].id)
                    for k, v in fields.items():
                        setattr(a, k, v)
                    s.add(a)
                    s.commit()
                media_index.store_vec(a.id, a.embedding)
            else:
                _store(MediaAsset(source="workspace", source_id=ids[p], run_id=run_id, license="own",
                                  model=r.get("model") or model, **fields))
    return usable, problems


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def _assets(ids: list[str]) -> dict[str, MediaAsset]:
    if not ids:
        return {}
    with session() as s:
        return {a.id: a for a in s.exec(select(MediaAsset).where(MediaAsset.id.in_(ids)))}  # type: ignore[attr-defined]


def rank(pool: list[MediaAsset], text_vec: list[float], picked: list[MediaAsset], k: int) -> list[dict[str, Any]]:
    """The story-state ranking (see the module docstring), best first."""
    vecs = [a.embedding for a in picked if a.embedding]
    centroid = _unit([sum(col) / len(vecs) for col in zip(*vecs)]) if vecs else None
    creators = {a.creator_id for a in picked if a.creator_id}
    picked_ids = {a.id for a in picked}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in pool:
        if a.id in picked_ids or a.sha256 in seen or not a.embedding:
            continue
        if any(media_index.cosine(a.embedding, v) > NEAR_DUPLICATE for v in vecs):
            continue
        seen.add(a.sha256)
        text = media_index.cosine(a.embedding, text_vec)
        style = media_index.cosine(a.embedding, centroid) if centroid else 0.0
        same = bool(a.creator_id and a.creator_id in creators)
        out.append({"asset": a, "score": text + STYLE_WEIGHT * style + (CREATOR_BONUS if same else 0.0),
                    "text_score": text, "style_score": style, "same_creator": same})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:k]


def _show(c: dict[str, Any]) -> dict[str, Any]:
    a: MediaAsset = c["asset"]
    out = {"asset_id": a.id, "source": a.source, "creator": a.creator_name, "width": a.width, "height": a.height,
           "alt": a.alt, "preview": a.url if a.source == "pexels" else a.source_id.split("/", 1)[1],
           "score": round(c["score"], 4), "text_score": round(c["text_score"], 4),
           "style_score": round(c["style_score"], 4)}
    if c["same_creator"]:
        out["same_creator"] = True
    return out


@todd_tool(toolset="video")
async def video_find_shots(slug: str, shot_id: str, k: int = 6, sources: list[str] | None = None,
                           query: str | None = None, workspace_paths: list[str] | None = None) -> dict:
    """Find images for one shot, by meaning: Pexels stock photos and the run's own images (everything in
    video/library/ plus workspace_paths). Ranked by fit to the shot's text and, once shots are picked, by how well
    they match the look of the other picked shots (score = text_score + a weight × style_score). Pick one with
    video_pick.

    Args:
        slug: the storyboard, from video_new
        shot_id: e.g. "s2"
        k: how many options to return (1–12)
        sources: "pexels" and/or "workspace" (default both)
        query: search words to use instead of the shot's text, e.g. "ice hockey skates close up"
        workspace_paths: more images in the run folder to consider, e.g. ["app/screenshots/home.png"]
    """
    ctx = get_ctx()
    run_id = ctx.run_id
    sources = list(dict.fromkeys(sources or SOURCES))
    if not sources or set(sources) - set(SOURCES):
        raise ToolError(f"sources are {' and/or '.join(SOURCES)}")
    k = max(1, min(int(k), 12))
    named = list(dict.fromkeys(_rel(p) for p in workspace_paths or []))
    board = await _load(slug)
    shot = _shot(board, shot_id)
    key = None
    if "pexels" in sources:
        key = vault.get_secret("PEXELS_API_KEY")
        if not key:
            raise ToolError(NO_PEXELS)
    words = " ".join((query or shot["text"]).split())
    full = f"{words}, {board['style_note']}" if board.get("style_note") else words
    t = await media.embed_text([full])
    text_vec, model = t["vectors"][0], t.get("image_model") or t.get("model") or ""

    pool: list[MediaAsset] = []
    problems: list[str] = []
    if "workspace" in sources:
        library = await _library()
        paths = list(dict.fromkeys(library + named))
        usable, bad = await _workspace(run_id, paths, model) if paths else (set(), [])
        problems += bad
        failed = {f"{run_id}/{p}" for p in paths} - usable  # checked just now: missing or not an image
        current = set(library)
        # the run's own library: what it embedded (this call or earlier), less what has left video/library/ since
        for a, _ in media_index.nearest(text_vec, LIBRARY_MAX + len(named) + 40, source="workspace", model=model,
                                        run_id=run_id):
            path = a.source_id.split("/", 1)[1]
            gone = path.startswith(LIBRARY_DIR + "/") and path not in current
            if a.source_id not in failed and not gone:
                pool.append(a)
    if key:
        found, bad = await _pexels(run_id, key, words, model)
        pool += found
        problems += bad

    # the story state is the other shots' picks (searching a picked shot again offers alternatives to its own)
    picked_ids = [s["pick"]["asset_id"] for s in board["shots"] if s.get("pick") and s["id"] != shot_id]
    picked = [a for a in _assets(picked_ids).values() if a.model == model]
    ranked = rank(pool, text_vec, picked, k)

    async with _lock(slug):
        board = await _load(slug)
        shot = _shot(board, shot_id)
        new_ids = [c["asset"].id for c in ranked]
        shot["candidates"] = list(dict.fromkeys(new_ids + list(shot.get("candidates") or [])))[:MAX_CANDIDATES]
        scores = {**(shot.get("scores") or {}), **{c["asset"].id: round(c["score"], 4) for c in ranked}}
        shot["scores"] = {i: scores[i] for i in shot["candidates"] if i in scores}
        await _save(board)
    _emit(f"Found {len(ranked)} options for {shot_id}",
          {"slug": slug, "shot_id": shot_id, "query": full, "candidates": new_ids,
           "top": _show(ranked[0]) if ranked else None})
    out: dict[str, Any] = {"shot": {"id": shot_id, "text": shot["text"], "caption": shot["caption"]}, "query": full,
                           "state": _state(board), "candidates": [_show(c) for c in ranked]}
    if problems:
        out["skipped"] = problems[:10]
    if not ranked:
        out["hint"] = ("Nothing found. Try a shorter, more concrete query, or put images in video/library/ "
                       "(or pass workspace_paths).")
    return out


# ------------------------------------------------------------------------------------------ pick and render
@todd_tool(toolset="video")
async def video_pick(slug: str, shot_id: str, asset_id: str) -> dict:
    """Use one of a shot's candidates (from video_find_shots) for that shot. Its file is copied to
    video/<slug>/assets/<shot_id>.<ext>. Picking again replaces the earlier pick.

    Args:
        slug: the storyboard, from video_new
        shot_id: e.g. "s2"
        asset_id: a candidate's asset_id
    """
    run_id = get_ctx().run_id
    async with _lock(slug):
        board = await _load(slug)
        shot = _shot(board, shot_id)
        if asset_id not in (shot.get("candidates") or []):
            raise ToolError(f"{asset_id} isn't one of {shot_id}'s candidates: pick an asset_id that "
                            f"video_find_shots returned for {shot_id}")
        a = _assets([asset_id]).get(asset_id)
        if a is None:
            raise ToolError(f"asset {asset_id} not found: search again with video_find_shots")
        if a.source == "workspace":
            src = a.source_id.split("/", 1)[1]
        else:  # the cache is shared by every run, the files aren't: fetch it into this run's folder if needed
            src = (await media.fetch(run_id, a.url or "", CACHE_DIR, sha256=a.sha256))["path"]
        ext = posixpath.splitext(src)[1].lower() or ".jpg"
        dest = f"video/{slug}/assets/{shot_id}{ext}"
        stem = shlex.quote(f"video/{slug}/assets/{shot_id}")
        r = await sandbox.exec_(f"mkdir -p {shlex.quote(posixpath.dirname(dest))} && rm -f {stem}.* && "
                                f"cp -- {shlex.quote(src)} {shlex.quote(dest)}", cwd=_root(), timeout=60)
        if r.get("exit_code") != 0:
            raise ToolError(f"couldn't copy {src}: {str(r.get('output') or '')[:300]}")
        shot["pick"] = {"asset_id": asset_id, "path": dest, "score": (shot.get("scores") or {}).get(asset_id)}
        shot["status"] = "picked"
        await _save(board)
    open_shots = [s["id"] for s in board["shots"] if not s.get("pick")]
    _emit(f"Picked {a.source} image for {shot_id}", {"slug": slug, "shot_id": shot_id, "asset_id": asset_id,
                                                      "path": dest})
    return {"shot_id": shot_id, "path": dest, "state": _state(board), "open_shots": open_shots,
            "next": f"video_find_shots(\"{slug}\", \"{open_shots[0]}\")" if open_shots
            else f"video_render(\"{slug}\")"}


def _credits(board: dict[str, Any], assets: dict[str, MediaAsset]) -> str:
    lines = [f"# Credits: {board['title']}", ""]
    stock: list[tuple[dict[str, Any], MediaAsset]] = []
    own: list[tuple[dict[str, Any], MediaAsset | None]] = []
    for s in board["shots"]:
        a = assets.get(s["pick"]["asset_id"])
        if a is not None and a.source == "pexels":
            stock.append((s, a))
        else:
            own.append((s, a))
    if stock:
        lines += ["Photos from [Pexels](https://www.pexels.com) (free to use under the Pexels license; credit is "
                  "appreciated).", ""]
        for s, a in stock:
            who = f"[{a.creator_name}]({a.creator_url})" if a.creator_name and a.creator_url \
                else (a.creator_name or "?")
            lines.append(f"- {s['id']}: photo by {who} on Pexels: https://www.pexels.com/photo/{a.source_id}/")
        lines.append("")
    if own:
        lines += ["The run's own images:", ""]
        lines += [f"- {s['id']}: {a.source_id.split('/', 1)[1] if a else s['pick']['path']}" for s, a in own]
        lines.append("")
    return "\n".join(lines)


@todd_tool(toolset="video")
async def video_render(slug: str, music_path: str | None = None, motion: str = "kenburns") -> dict:
    """Render the slideshow once every shot is picked: the MP4 (video/<slug>/<slug>.mp4, 1080×1920, 30 fps, each
    slide on screen for its duration_s), the captioned slides it's made of (video/<slug>/slides/01.png, …, also
    usable as a TikTok photo post), all of them side by side in video/<slug>/preview.png, and CREDITS.md. Stock
    photos fill the slide; the run's own images (screenshots) are shown whole. Rendering again replaces them.

    Args:
        slug: the storyboard, from video_new
        music_path: an audio file in the run folder to play under it (trimmed to the video, faded out at the end).
            Only music the human gave you or that you know is licensed for this use; default none.
        motion: "kenburns" (a slow zoom on every slide, the default) or "none" (still slides)
    """
    run_id = get_ctx().run_id
    if motion not in MOTIONS:
        raise ToolError(f"motion is {' or '.join(MOTIONS)}")
    music = _rel(music_path, AUDIO_EXT, "audio files") if music_path else None
    board = await _load(slug)
    open_shots = [s["id"] for s in board["shots"] if not s.get("pick")]
    if open_shots:
        raise ToolError(f"pick every shot first: {', '.join(open_shots)} still open (video_find_shots, video_pick)")
    assets = _assets([s["pick"]["asset_id"] for s in board["shots"]])
    # stock photos fill the slide; the run's own images (app screenshots) are shown whole
    slides = [{"image": s["pick"]["path"], "caption": s["caption"], "caption_position": s["caption_position"],
               "fit": "cover" if getattr(assets.get(s["pick"]["asset_id"]), "source", "") == "pexels" else "contain"}
              for s in board["shots"]]
    r = await media.compose(run_id, slides, f"video/{slug}/slides", board["width"], board["height"],
                            sheet=f"video/{slug}/preview.png")
    credits = f"video/{slug}/CREDITS.md"
    await sandbox.write(_abs(credits), _credits(board, assets))
    try:
        v = await media.render(run_id, r["slides"], [float(s["duration_s"]) for s in board["shots"]],
                               f"video/{slug}/{slug}.mp4", fps=FPS, motion=motion, music=music,
                               width=board["width"], height=board["height"])
    except ToolError as e:
        raise ToolError(f"the slides are ready in video/{slug}/slides/, but the MP4 failed: {e}") from e
    out = {"video": v["path"], "duration_s": v["duration_s"], "slides": r["slides"], "preview": r.get("sheet"),
           "credits": credits, "storyboard": _board_path(slug)}
    _emit(f"Rendered {slug}.mp4: {len(r['slides'])} slides, {v['duration_s']:g}s", {**out, "music": music})
    return out


VIDEO_TOOLS = [video_new, video_find_shots, video_pick, video_render]
