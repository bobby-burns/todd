"""The `shorts` toolset: short vertical videos with a voiceover, timed from the voice.

Order (docs/video-step2-plan.md, "Locking voice to picture"):
  short_record             film the product in the agents' browser (Todd built it, so it records its own footage)
  short_new / short_edit   the script: hook variants and beats, one voiceover line, on-screen text and shot each
  short_voiceover          one take per hook variant with character timestamps: the free local voice for the scaffold,
                           ElevenLabs for the final (the voice lock)
  short_plan               every cut, caption, hold and clip length from the take, a sync report, the generation price
  short_render             the scaffold/animatic (AI shots as labelled start frames) or the final cut
  short_review             show the human a numbered scaffold version and get their call: approve, or what to change
  short_pace               how fast it moves (voice speed, pause between lines, time per shot, words per caption)

Everything lives in the run folder under video/<slug>/: script.json, audio/vo-<hook>.mp3 + .json (the take and its
timestamps), timeline-<hook>.json (the plan), and the MP4s. Nothing here generates AI video yet; AI shots stay
placeholders, priced, until the human has approved the animatic.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from .. import screencast
from .. import shorts_plan as sp
from ..policy import SpendDenied, authorize_spend, settle
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from . import elevenlabs, media, sandbox
from .human import secret_or_ask
from .sandbox_tools import _abs, _root
from .video import IMAGE_EXT, _rel, _slugify

SOURCES = ("screen", "clip", "image", "ai")
VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")
MAX_HOOKS, MAX_BEATS = 3, 10
MAX_VO_CHARS, MAX_TEXT_CHARS, MAX_PROMPT = 160, 60, 600
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
PART_ID = re.compile(r"(h|b)[0-9]{1,2}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
PROVIDERS = ("local", "elevenlabs")
_locks: dict[str, asyncio.Lock] = {}


# ------------------------------------------------------------------------------------------ files
def _dir(slug: str) -> str:
    return f"video/{slug}"


def _lock(slug: str) -> asyncio.Lock:
    return _locks.setdefault(f"{get_ctx().run_id}/{slug}", asyncio.Lock())


async def _read_json(path: str) -> dict[str, Any] | None:
    try:
        r = await sandbox.call("/files/view", {"base": _root(), "path": path, "raw": True}, timeout=30)
    except ToolError as e:
        if "-> 404" in str(e):
            return None
        raise
    return json.loads(base64.b64decode(r["data"]))


async def _write_json(path: str, data: dict[str, Any]) -> None:
    await sandbox.write(_abs(path), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


async def _load(slug: str) -> dict[str, Any]:
    if not SLUG.fullmatch(slug or ""):
        raise ToolError(f"bad slug {slug!r}: use the one short_new returned")
    s = await _read_json(f"{_dir(slug)}/script.json")
    if s is None:
        raise ToolError(f"no short {slug!r} in this run: start one with short_new")
    return s


def _emit(text: str, data: dict[str, Any]) -> None:
    get_ctx().emit(get_agent_id(), "step", text, data)


LOCK_WAIT_S = 180  # how long a recording waits for another agent to finish with the browser


@asynccontextmanager
async def _browser(what: str) -> AsyncIterator[None]:
    """Hold the one agents' browser for a recording. An agent that already has it (its own browser_start session)
    records straight away instead of waiting on itself; waiting on someone else is announced, and gives up after
    LOCK_WAIT_S with who has it."""
    ctx, agent = get_ctx(), get_agent_id()
    lock, mine = ctx.browser_lock, f"{ctx.run_id}:{agent}"
    if lock.locked() and lock.holder == mine:
        yield
        return
    holder = f"{mine} {what}"
    if lock.locked():
        who = lock.holder or "someone"
        who = (f"another agent in this run ({who.split(':', 1)[1]})" if who.startswith(f"{ctx.run_id}:")
               else "another run")
        _emit(f"Waiting for the browser ({who} is using it)…", {"waiting_for": lock.holder})
        try:
            await asyncio.wait_for(lock.acquire(holder), LOCK_WAIT_S)
        except asyncio.TimeoutError:
            raise ToolError(f"The browser has been busy for {LOCK_WAIT_S // 60} minutes ({who} is using it). Do "
                            "the work that doesn't need it (script, voice, plan) and record after.") from None
    else:
        await lock.acquire(holder)
    try:
        yield
    finally:
        lock.release(holder)


def _show(b64: str | None, mime: str = "image/png") -> None:
    """Let the agent see an image with this tool's result (a contact sheet, a screenshot)."""
    if b64:
        get_ctx().push_image(get_agent_id(), b64, mime)


async def _sheet(src: str, out: str, times: list[float], labels: list[str]) -> str | None:
    """A labelled contact sheet of `src` at `times`, shown to the agent; its path, or None if it couldn't be made."""
    pairs = sorted({round(t, 2): lab for t, lab in zip(times, labels)}.items())[:12]
    try:
        r = await media.call("/frames", {"run_id": get_ctx().run_id, "src": src, "out": out, "inline": True,
                                         "times": [t for t, _ in pairs], "labels": [lab for _, lab in pairs]})
    except ToolError:
        return None
    _show(r.get("png_b64"))
    return r.get("sheet")


# ------------------------------------------------------------------------------------------ the script
def _line(value: Any, what: str, limit: int, required: bool = True) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ToolError(f"{what} is empty")
    if len(text) > limit:
        raise ToolError(f"{what} is {len(text)} characters; keep it to {limit}")
    if re.search(r"[\[\]<>{}]", text):
        raise ToolError(f"{what}: plain words only (no [tags], <ssml> or {{}}); the delivery comes from the voice")
    return text


def _shot(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("source") not in SOURCES:
        raise ToolError(f"{where}: shot needs a source: {', '.join(SOURCES)}")
    src = raw["source"]
    shot: dict[str, Any] = {"source": src}
    if src == "ai":
        shot["prompt"] = _line(raw.get("prompt"), f"{where}: the AI shot's prompt", MAX_PROMPT)
        model = raw.get("model") or sp.DEFAULT_AI_MODEL
        if model not in sp.AI_MODELS:
            raise ToolError(f"{where}: model is one of {', '.join(sp.AI_MODELS)}")
        shot["model"] = model
        if raw.get("start_image"):
            shot["start_image"] = _rel(raw["start_image"])
    else:
        if not raw.get("path"):
            raise ToolError(f"{where}: a {src} shot needs the path of its file in the run folder")
        ext, what = (IMAGE_EXT, "images") if src == "image" else (VIDEO_EXT, "videos")
        shot["path"] = _rel(raw["path"], ext, what)
    if raw.get("in_s") is not None:
        shot["in_s"] = max(0.0, float(raw["in_s"]))
    if raw.get("sync"):
        sync = raw["sync"]
        if not isinstance(sync, dict) or not sync.get("word") or sync.get("at_s") is None:
            raise ToolError(f"{where}: sync is {{\"word\": a word from this beat's line, \"at_s\": seconds into the "
                            "clip}")
        shot["sync"] = {"word": str(sync["word"]), "at_s": float(sync["at_s"])}
    speed = raw.get("speed")
    if speed is not None:
        if speed != "fit" and not (isinstance(speed, (int, float)) and 0.5 <= speed <= 2):
            raise ToolError(f"{where}: speed is a number from 0.5 to 2, or \"fit\" (speed the recording up to fit)")
        shot["speed"] = speed
    for k, allowed in (("fit", ("cover", "contain")), ("zoom", ("in", "out", "none"))):
        if raw.get(k) is not None:
            if raw[k] not in allowed:
                raise ToolError(f"{where}: {k} is {' or '.join(allowed)}")
            shot[k] = raw[k]
    return shot


def _part(raw: Any, pid: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ToolError(f"{pid}: each hook and beat is an object with vo, text and shot")
    part = {"id": pid, "vo": _line(raw.get("vo"), f"{pid}'s vo", MAX_VO_CHARS),
            "text": _line(raw.get("text"), f"{pid}'s text", MAX_TEXT_CHARS, required=False),
            "shot": _shot(raw.get("shot"), pid)}
    hold = float(raw.get("hold_s") or 0)
    if not 0 <= hold <= 3:
        raise ToolError(f"{pid}: hold_s is 0–3 seconds")
    if hold:
        part["hold_s"] = hold
    return part


def _check_length(script: dict[str, Any]) -> list[str]:
    est = sp.estimate_seconds(script)
    rules = sp.PLATFORMS[script["platform"]]
    if est > rules["max_s"]:
        raise ToolError(f"this script runs about {est:.0f}s; {script['platform']} allows {rules['max_s']}s")
    return sp.length_notes(est, rules, about="about ")


@todd_tool(toolset="shorts")
async def short_new(title: str, hooks: list[dict], beats: list[dict], format_id: str | None = None,
                    platform: str = "tiktok", voice_id: str | None = None, voice_model: str | None = None,
                    end_hold_s: float = 0.6, pace: dict | None = None) -> dict:
    """Start a short: writes its script to video/<slug>/script.json. Follow a format card (format_search, or a fresh
    trend scan): its hook, its beats, its sound. Write the way people in the niche talk. Not a feature tour: the
    product is the payoff of the format, not a list of what it does.

    Args:
        title: what it is, e.g. "drop-in roster skit"
        format_id: the format card this script follows (from format_search / format_save); shown to the human at
            review. Leave it out only if the human asked for something else.
        hooks: 1–3 variants of the opening (each becomes its own cut for A/B): {"vo": spoken line, "text": optional
            on-screen text, "shot": {...}}
        beats: 1–10 beats after the hook, in order: {"vo": one spoken line (max 160 chars), "text": optional on-screen
            text (max 60), "shot": {...}, "hold_s": optional extra seconds after the line (e.g. a visual payoff)}.
            A shot is {"source": "screen" | "clip" | "image", "path": file in the run folder} or {"source": "ai",
            "prompt": what to generate, "start_image": optional still to start from, "model": "seedance-2.5"}.
            Optional on any shot: "in_s" (start that far into the clip), "sync": {"word": a word of this line,
            "at_s": the moment in the clip to land on it}, "speed": "fit" (speed a long recording up to fit), "fit":
            "contain" (show a non-9:16 frame whole).
        platform: "tiktok", "reels" or "shorts"
        voice_id: an ElevenLabs voice (see short_voices); chosen at voiceover time if not given
        voice_model: ElevenLabs model, default eleven_multilingual_v2
        end_hold_s: seconds after the last word before the video ends
        pace: optional {"voice_speed": 0.8–1.2, "beat_gap_s": pause after each line, "min_shot_s": least time any
            shot stays on screen, "caption_words": 1–4}; defaults are calm enough to follow on a first watch
    """
    if platform not in sp.PLATFORMS:
        raise ToolError(f"platform is one of {', '.join(sp.PLATFORMS)}")
    if not (title or "").strip():
        raise ToolError("give the short a title")
    if not isinstance(hooks, list) or not 1 <= len(hooks) <= MAX_HOOKS:
        raise ToolError(f"1–{MAX_HOOKS} hooks")
    if not isinstance(beats, list) or not 1 <= len(beats) <= MAX_BEATS:
        raise ToolError(f"1–{MAX_BEATS} beats")
    if voice_model and voice_model not in elevenlabs.MODELS:
        raise ToolError(f"voice_model is one of {', '.join(elevenlabs.MODELS)}")
    if not 0 <= float(end_hold_s) <= 3:
        raise ToolError("end_hold_s is 0–3 seconds")
    script = {"version": 1, "title": title.strip(), "platform": platform, "width": 1080, "height": 1920,
              "fps": sp.FPS, "end_hold_s": float(end_hold_s),
              "voice": {"voice_id": voice_id, "model_id": voice_model or elevenlabs.DEFAULT_MODEL},
              "hooks": [_part(h, f"h{i}") for i, h in enumerate(hooks, 1)],
              "beats": [_part(b, f"b{i}") for i, b in enumerate(beats, 1)], "takes": {}, "reviews": []}
    script["pace"] = sp.pace(script, change={k: v for k, v in (pace or {}).items() if k in sp.DEFAULT_PACE})
    warnings = _check_length(script)
    if format_id:
        from ..db import FormatCard, session

        with session() as db:
            card = db.get(FormatCard, format_id)
        if card is None:
            raise ToolError(f"no format card {format_id!r}: use an id from format_search or format_save")
        script["format"] = {"id": card.id, "name": card.name, "examples": len(card.examples),
                            "plays": [e.get("plays") for e in card.examples][:5]}
    else:
        warnings.append("no format card: shorts that follow a format seen working in the niche do better than "
                        "feature tours (trends: format_search)")
    base = _slugify(title)
    slug, n = base, 1
    while (await _read_json(f"{_dir(slug)}/script.json")) is not None \
            or (await _read_json(f"{_dir(slug)}/storyboard.json")) is not None:
        n += 1
        slug = f"{base[:44]}-{n}"
    script["slug"] = slug
    await _write_json(f"{_dir(slug)}/script.json", script)
    _emit(f"Short {slug}: {len(script['hooks'])} hooks, {len(script['beats'])} beats", {"slug": slug})
    return {"slug": slug, "path": f"{_dir(slug)}/script.json", "estimate_s": round(sp.estimate_seconds(script), 1),
            "warnings": warnings,
            "next": "Show the script to the human; adjust with short_edit; then short_voiceover and short_plan."}


@todd_tool(toolset="shorts")
async def short_edit(slug: str, part: str, vo: str | None = None, text: str | None = None,
                     shot: dict | None = None, hold_s: float | None = None) -> dict:
    """Change one hook or beat of a short. Changing a voiceover line means voicing it again (short_voiceover);
    changing a shot, on-screen text or hold doesn't.

    Args:
        slug: the short, from short_new
        part: "h1".."h3" or "b1".."b10"
        vo: the new spoken line
        text: the new on-screen text ("" to remove it)
        shot: the new shot (same format as in short_new)
        hold_s: extra seconds after the line (0 to remove)
    """
    if not PART_ID.fullmatch(part or ""):
        raise ToolError("part is a hook id (h1…) or a beat id (b1…)")
    async with _lock(slug):
        script = await _load(slug)
        p = next((x for x in [*script["hooks"], *script["beats"]] if x["id"] == part), None)
        if p is None:
            raise ToolError(f"no {part} in {slug}")
        changed = []
        if vo is not None:
            new = _line(vo, f"{part}'s vo", MAX_VO_CHARS)
            if new != p["vo"]:
                p["vo"] = new
                changed.append("vo")
        if text is not None:
            p["text"] = _line(text, f"{part}'s text", MAX_TEXT_CHARS, required=False)
            changed.append("text")
        if shot is not None:
            p["shot"] = _shot(shot, part)
            changed.append("shot")
        if hold_s is not None:
            if not 0 <= float(hold_s) <= 3:
                raise ToolError("hold_s is 0–3 seconds")
            p.pop("hold_s", None)
            if hold_s:
                p["hold_s"] = float(hold_s)
            changed.append("hold_s")
        warnings = _check_length(script)
        await _write_json(f"{_dir(slug)}/script.json", script)
    stale = [h for h, t in (script.get("takes") or {}).items() if _take_stale(script, h, t)]
    return {"part": part, "changed": changed, "warnings": warnings, "voice_out_of_date": stale,
            "next": "short_voiceover, then short_plan" if stale else "short_plan"}


def _take_stale(script: dict[str, Any], hook: str, take: dict[str, Any]) -> bool:
    """The take no longer matches the script: a line changed, or the voice speed did."""
    try:
        text = sp.take_text(script, hook)[0]
    except ValueError:
        return True
    return text != take.get("text") or abs(float(take.get("speed", 1.0)) - sp.pace(script)["voice_speed"]) > 1e-6


# ------------------------------------------------------------------------------------------ the voice
async def _key() -> str:
    return await secret_or_ask("ELEVENLABS_API_KEY", elevenlabs.ASK_KEY)


@todd_tool(toolset="shorts")
async def short_voices(search: str | None = None) -> dict:
    """List the ElevenLabs voices this account can use, to pick one for a short.

    Args:
        search: words to match against name, description and labels, e.g. "young male casual"
    """
    vs = await elevenlabs.voices(await _key(), search)
    return {"voices": vs[:25], "count": len(vs)}


@todd_tool(toolset="shorts")
async def short_voiceover(slug: str, hooks: list[str] | None = None, provider: str = "local",
                          voice_id: str | None = None) -> dict:
    """Voice the short: one continuous take per hook variant (hook + beats), with word timestamps. The take is the
    master clock: cuts, captions and clip lengths are planned from it (short_plan). Start with the free local voice for
    the scaffold; once the human approves the scaffold, voice it with ElevenLabs (cents, one charge for all takes) and
    plan again before generating anything.

    Args:
        slug: the short, from short_new
        hooks: which hook variants to voice (default all)
        provider: "local" (free, for the scaffold) or "elevenlabs" (the final voice)
        voice_id: an ElevenLabs voice (see short_voices); default the script's, else a premade narration voice
    """
    if provider not in PROVIDERS:
        raise ToolError(f"provider is {' or '.join(PROVIDERS)}")
    ctx = get_ctx()
    script = await _load(slug)
    ids = [h["id"] for h in script["hooks"]]
    todo = hooks or ids
    unknown = [h for h in todo if h not in ids]
    if unknown:
        raise ToolError(f"no hook {', '.join(unknown)}: hooks are {', '.join(ids)}")
    if provider == "local":
        return await _voice_local(slug, todo)
    key = await _key()
    voice = voice_id or (script.get("voice") or {}).get("voice_id")
    if not voice:
        premade = [v for v in await elevenlabs.voices(key) if v.get("category") == "premade"]
        if not premade:
            raise ToolError("pick a voice with short_voices and pass voice_id")
        voice = premade[0]["voice_id"]
    model = (script.get("voice") or {}).get("model_id") or elevenlabs.DEFAULT_MODEL
    texts = {h: sp.take_text(script, h)[0] for h in todo}
    chars = sum(len(t) for t in texts.values())
    usd = max(0.01, math.ceil(elevenlabs.cost_usd(chars, model) * 100) / 100)
    try:
        entry = await authorize_spend(ctx, amount_usd=usd, merchant="ElevenLabs",
                                      description=f"Voiceover for short {slug}: {len(todo)} take(s), "
                                                  f"{chars} characters",
                                      data={"slug": slug, "hooks": todo, "characters": chars, "model": model})
    except SpendDenied as e:
        raise ToolError(str(e)) from e
    done: dict[str, Any] = {}
    try:
        for h in todo:
            r = await elevenlabs.speak(key, voice, texts[h], model,
                                       settings={"speed": min(max(sp.pace(script)["voice_speed"], 0.7), 1.2)})
            audio = f"{_dir(slug)}/audio/vo-{h}.mp3"
            await media.put(ctx.run_id, audio, r["audio"])
            take = {"provider": "elevenlabs", "text": texts[h], "voice_id": voice, "model_id": model,
                    "chars": len(texts[h]), "request_id": r.get("request_id"), "audio": audio,
                    "alignment": r["alignment"]}
            await _write_json(f"{_dir(slug)}/audio/vo-{h}.json", take)
            ends = r["alignment"].get("character_end_times_seconds") or [0]
            done[h] = {"audio": audio, "timestamps": f"{_dir(slug)}/audio/vo-{h}.json",
                       "spoken_s": round(float(ends[-1]), 2)}
    except Exception as e:
        settle(entry, "failed" if not done else "completed", {"error": str(e)[:300], "voiced": list(done)})
        raise
    settle(entry, "completed", {"voiced": list(done)})
    async with _lock(slug):
        script = await _load(slug)
        script["voice"] = {**(script.get("voice") or {}), "voice_id": voice}
        takes = script.setdefault("takes", {})
        for h, d in done.items():
            takes[h] = {"audio": d["audio"], "timestamps": d["timestamps"], "text": texts[h], "provider": "elevenlabs",
                        "speed": sp.pace(script)["voice_speed"]}
        await _write_json(f"{_dir(slug)}/script.json", script)
    _emit(f"Voiced {slug}: {', '.join(done)}", {"slug": slug, "takes": done, "usd": usd})
    return {"takes": done, "voice_id": voice, "usd": usd, "next": f"short_plan(\"{slug}\")"}


async def _voice_local(slug: str, todo: list[str]) -> dict:
    """The free scaffold voice: Piper in the media service, timed by a local recogniser."""
    run_id = get_ctx().run_id
    script = await _load(slug)
    speed = sp.pace(script)["voice_speed"]
    done: dict[str, Any] = {}
    for h in todo:
        text = sp.take_text(script, h)[0]
        audio = f"{_dir(slug)}/audio/vo-{h}.wav"
        r = await media.tts_local(run_id, text, audio, speed=speed)
        await _write_json(f"{_dir(slug)}/audio/vo-{h}.json", {"provider": "local", "text": text, "voice": r["voice"],
                                                             "audio": audio, "alignment": r["alignment"]})
        done[h] = {"audio": audio, "timestamps": f"{_dir(slug)}/audio/vo-{h}.json", "spoken_s": r["duration_s"]}
    async with _lock(slug):
        script = await _load(slug)
        takes = script.setdefault("takes", {})
        for h, d in done.items():
            takes[h] = {"audio": d["audio"], "timestamps": d["timestamps"], "text": sp.take_text(script, h)[0],
                        "provider": "local", "speed": speed}
        await _write_json(f"{_dir(slug)}/script.json", script)
    _emit(f"Voiced {slug} (free scaffold voice): {', '.join(done)}", {"slug": slug, "takes": done})
    return {"takes": done, "provider": "local", "usd": 0,
            "next": f"short_plan(\"{slug}\"), then short_render for the scaffold"}


# ------------------------------------------------------------------------------------------ plan and render
async def _plan(slug: str, hooks: list[str] | None) -> tuple[dict[str, Any], dict[str, sp.Plan]]:
    run_id = get_ctx().run_id
    script = await _load(slug)
    takes = script.get("takes") or {}
    todo = hooks or [h["id"] for h in script["hooks"] if h["id"] in takes]
    if not todo:
        raise ToolError(f"{slug} isn't voiced yet: run short_voiceover first")
    for h in todo:
        if h not in takes:
            raise ToolError(f"{h} isn't voiced yet: short_voiceover(\"{slug}\", hooks=[\"{h}\"])")
        if _take_stale(script, h, takes[h]):
            raise ToolError(f"the script changed since {h} was voiced: run short_voiceover(\"{slug}\") again")
    paths = sorted({s.get(k) for p in [*script["hooks"], *script["beats"]] for s in [p["shot"]]
                    for k in ("path", "clip") if s.get(k)})
    info = await media.probe(run_id, paths)
    plans: dict[str, sp.Plan] = {}
    for h in todo:
        take = await _read_json(takes[h]["timestamps"])
        if take is None:
            raise ToolError(f"{takes[h]['timestamps']} is missing: voice {h} again")
        try:
            plans[h] = sp.plan(script, h, take["alignment"], info, takes[h]["audio"])
        except ValueError as e:
            raise ToolError(f"{h}: {e}") from e
    return script, plans


@todd_tool(toolset="shorts")
async def short_plan(slug: str, hook: str | None = None) -> dict:
    """Plan every cut, caption, hold and clip length from the voiced takes, and price the AI shots. Writes
    video/<slug>/timeline-<hook>.json. Fix the warnings (shorter lines, hold_s, shot.sync, longer recordings) and plan
    again before rendering.

    Args:
        slug: the short, from short_new
        hook: one hook variant (default every voiced one)
    """
    script, plans = await _plan(slug, [hook] if hook else None)
    for h, p in plans.items():
        await _write_json(f"{_dir(slug)}/timeline-{h}.json", {**p.timeline, "report": p.report})
    specs = sp.merge_generation(list(plans.values()))
    out = {"cuts": {h: {"duration_s": p.report["duration_s"], "warnings": p.report["warnings"],
                        "beats": [{k: b[k] for k in ("id", "start_s", "end_s", "fit")} for b in p.report["beats"]]}
                    for h, p in plans.items()},
           "generate": specs, "generate_usd": sp.total_usd(specs),
           "next": f"short_render(\"{slug}\", \"{next(iter(plans))}\") for the animatic"}
    lengths = ", ".join(f"{h} {p.report['duration_s']:.1f}s" for h, p in plans.items())
    _emit(f"Planned {slug}: {lengths}", {"slug": slug, "generate_usd": out["generate_usd"],
                                         "warnings": sum(len(p.report["warnings"]) for p in plans.values())})
    return out


@todd_tool(toolset="shorts")
async def short_render(slug: str, hook: str, mode: str = "animatic") -> dict:
    """Render one cut. "animatic": the real voice, captions and recordings, with each AI shot as its labelled start
    frame and price, for the human to approve before anything is generated. "final": needs every AI shot generated.

    Args:
        slug: the short, from short_new
        hook: the hook variant, e.g. "h1"
        mode: "animatic" or "final"
    """
    if mode not in ("animatic", "final"):
        raise ToolError("mode is \"animatic\" or \"final\"")
    _, plans = await _plan(slug, [hook])
    p = plans[hook]
    pending = [g["beat"] for g in p.generate]
    if mode == "final" and pending:
        raise ToolError(f"{', '.join(pending)} aren't generated yet: render the animatic and get it approved first")
    await _write_json(f"{_dir(slug)}/timeline-{hook}.json", {**p.timeline, "report": p.report})
    out = f"{_dir(slug)}/{slug}-{hook}{'-animatic' if mode == 'animatic' else ''}.mp4"
    r = await media.render_timeline(get_ctx().run_id, out, _with_tags(p.timeline) if mode == "animatic" else p.timeline)
    warnings = list(p.report["warnings"])
    script = await _load(slug)
    if mode == "final" and (script.get("takes") or {}).get(hook, {}).get("provider") == "local":
        warnings.append("this cut uses the free scaffold voice: for the final, "
                        "short_voiceover(provider=\"elevenlabs\") and render again")
    result = {"video": r["path"], "duration_s": r["duration_s"], "mode": mode, "warnings": warnings,
              "generate": p.generate, "generate_usd": sp.total_usd(p.generate),
              "sheet": await _cut_sheet(r["path"], p.report),
              "check": "look at the contact sheet (a frame from each beat). Would someone scrolling believe a person "
                       "filmed and edited this on their phone? Real screens or footage full-frame, no designed "
                       "backgrounds, mockups, logos or end cards, one short line of text, every beat showing what its "
                       "line says. Fix what doesn't pass before short_review."}
    _emit(f"Rendered {mode} {slug} {hook}: {r['duration_s']:g}s", {"slug": slug, "video": r["path"]})
    return result


@todd_tool(toolset="shorts")
async def short_record(name: str, url: str, steps: list[dict], device: str = "phone",
                       start_at: str | None = None, signed_in: bool = False) -> dict:
    """Film the product in the agents' browser. Todd built it, so record its real pages (the live site, or a local
    copy running in the sandbox at http://localhost:PORT) and its real flows: opens `url` in a phone-sized 9:16 tab
    (or "desktop"), runs the steps, and saves video/recordings/<name>.mp4 plus the time of every step, so a shot can
    land a moment on a spoken word (shot.sync.at_s). Rendering happens in Todd's media service: no ffmpeg or browser
    of your own is needed.

    It films in a fresh browser with nobody signed in, so it can do what any visitor does: tap answers, search, type
    into a demo form and submit it. It never pays, buys, subscribes, deletes, deploys or publishes, and never types a
    password. signed_in=true films pages behind the human's sign-in instead; then it only reads and makes safe taps
    (nothing that posts, sends or approves, no form submits), because there it would act as the human.

    Args:
        name: a short name for the recording, e.g. "week-view"
        url: the page to start on
        steps: what to do, in order: {"wait": seconds}, {"scroll": pixels or "bottom"}, {"scroll_to": "visible text"},
            {"tap": "button or link text"}, {"type": "text", "into": "field label or placeholder"}, {"goto": url},
            {"mark": "a name for this moment"}
        device: "phone" (1080×1920) or "desktop" (1440×810)
        start_at: visible text to bring to the top of the screen before filming starts, so the recording opens on
            the right part of the page (e.g. "Card checkouts")
        signed_in: film with the human's sign-ins (only for pages that need them)
    """
    if not NAME.fullmatch(name or ""):
        raise ToolError("name is lowercase letters, digits and dashes, e.g. \"week-view\"")
    try:
        screencast.check_steps(steps)
    except screencast.RecordError as e:
        raise ToolError(str(e)) from e
    ctx = get_ctx()
    _emit(f"Recording {name}: {url}", {"url": url, "steps": len(steps)})
    async with _browser(f"recording {name}"):
        try:
            rec = await screencast.record(url, steps, device, start_at=start_at, signed_in=bool(signed_in))
        except screencast.RecordError as e:
            _show(e.shot, "image/jpeg")  # the screen when it stopped
            raise ToolError(f"recording {name} stopped: {e}\nFix the steps from this (or short_look the page) "
                            "and record again.") from e
    session = uuid.uuid4().hex[:16]
    out = f"video/recordings/{name}.mp4"
    await media.cast_upload(ctx.run_id, session, [f for _, f in rec["frames"]])
    r = await media.cast_assemble(ctx.run_id, session, [t for t, _ in rec["frames"]], rec["duration_s"], out)
    meta = {"url": url, "device": device, "start_at": start_at, "signed_in": bool(signed_in), "steps": steps,
            "marks": rec["marks"],
            "duration_s": r["duration_s"],
            "frames": len(rec["frames"]), "width": rec["width"], "height": rec["height"]}
    await _write_json(f"video/recordings/{name}.json", meta)
    _emit(f"Recorded {name}: {r['duration_s']:g}s", {"path": out, "marks": rec["marks"]})
    # what it looks like at every step (just after each one, once it has happened on screen), for the agent to check
    moments = [(min(m["t"] + 0.7, r["duration_s"]), m["step"].split(" ", 1)[0]) for m in rec["marks"]]
    moments.append((r["duration_s"], "end"))
    sheet = await _sheet(out, f"video/recordings/{name}.png", [t for t, _ in moments], [lab for _, lab in moments])
    return {"path": out, "duration_s": r["duration_s"], "marks": rec["marks"], "sheet": sheet,
            "next": ("look at the contact sheet (a frame just after each step): is every moment what its beat needs? "
                     f"Then use it as a shot: {{\"source\": \"screen\", \"path\": \"{out}\"}}, and land a moment on "
                     "a word with \"sync\": {\"word\": …, \"at_s\": a mark's t}. A recording a little short for "
                     "its slot is fine: the plan holds its first or last frame.")}


async def _cut_sheet(video: str, report: dict[str, Any]) -> str | None:
    """One frame from each beat of a rendered cut, labelled b1, b2…, shown to the agent."""
    beats = report.get("beats") or []
    times = [b["start_s"] + 0.6 * (b["end_s"] - b["start_s"]) for b in beats]
    return await _sheet(video, re.sub(r"\.mp4$", ".png", video), times, [b["id"] for b in beats])


@todd_tool(toolset="shorts")
async def short_look(url: str, device: str = "phone", start_at: str | None = None, signed_in: bool = False) -> dict:
    """Look at a page before recording it: a screenshot of what a recording would open on, and an outline of the
    whole page (headings, things to tap, fields, each with how many screens down it is), so the steps you write
    find their texts the first time. Nothing is filmed or saved.

    Args:
        url: the page
        device: "phone" or "desktop"
        start_at: visible text to bring to the top first, as short_record would
        signed_in: look with the human's sign-ins (only for pages that need them)
    """
    async with _browser("looking at a page"):
        try:
            seen = await screencast.look(url, device, start_at=start_at, signed_in=bool(signed_in))
        except screencast.RecordError as e:
            _show(e.shot, "image/jpeg")
            raise ToolError(str(e)) from e
    _show(seen["shot"], "image/jpeg")
    return {"outline": screencast.outline_text(seen["outline"]),
            "next": "write short_record steps with these exact texts (tap / scroll_to / type into), and scroll to "
                    "anything more than a screen down before tapping it"}


def _with_tags(timeline: dict[str, Any]) -> dict[str, Any]:
    """A scaffold carries beat labels (b1 · 2.4s) so the human's feedback can point at a beat."""
    return {**timeline, "overlays": [*timeline.get("overlays", []), *timeline.get("tags", [])]}


@todd_tool(toolset="shorts")
async def short_pace(slug: str, preset: str | None = None, voice_speed: float | None = None,
                     beat_gap_s: float | None = None, min_shot_s: float | None = None,
                     caption_words: int | None = None) -> dict:
    """Change how fast the short moves, usually from the human's feedback on a scaffold. A preset nudges everything;
    numbers set one thing exactly. A new voice speed means voicing again (short_voiceover); the rest only needs
    short_plan.

    Args:
        slug: the short, from short_new
        preset: "slower" or "faster"
        voice_speed: 0.8–1.2 (1 = the voice's natural pace)
        beat_gap_s: pause after every line, 0–1.5 seconds
        min_shot_s: the least time any shot stays on screen, 0.6–4 seconds
        caption_words: words per caption page, 1–4
    """
    if preset not in (None, "slower", "faster"):
        raise ToolError("preset is \"slower\" or \"faster\"")
    async with _lock(slug):
        script = await _load(slug)
        before = sp.pace(script)
        script["pace"] = sp.pace(script, preset=preset, change={
            "voice_speed": voice_speed, "beat_gap_s": beat_gap_s, "min_shot_s": min_shot_s,
            "caption_words": caption_words})
        await _write_json(f"{_dir(slug)}/script.json", script)
    revoice = abs(script["pace"]["voice_speed"] - before["voice_speed"]) > 1e-6 and bool(script.get("takes"))
    return {"pace": script["pace"], "before": before,
            "next": "short_voiceover (the voice speed changed), then short_plan" if revoice else "short_plan"}


@todd_tool(toolset="shorts")
async def short_review(slug: str, hook: str = "h1") -> dict:
    """Show the human a free scaffold and get their call before anything costs money. Renders a numbered version
    (video/<slug>/<slug>-<hook>-scaffold-v<N>.mp4, beats labelled b1, b2… in the corner), asks them, and records their
    answer. "Slower" / "Faster" are applied for you; anything else is feedback for you to act on (short_edit,
    short_pace, a new recording), then voice, plan and review again. Only an approved scaffold goes on to the paid
    voice and generation.

    Args:
        slug: the short, from short_new
        hook: the hook variant to show
    """
    ctx = get_ctx()
    script, plans = await _plan(slug, [hook])
    p = plans[hook]
    if p.report["duration_s"] > sp.CEILING_S + 5:
        raise ToolError(f"this cut runs {p.report['duration_s']:.0f}s: get it under {sp.CEILING_S}s before showing it "
                        "to the human (shorter lines with short_edit, or short_new with fewer beats)")
    version = len(script.get("reviews") or []) + 1
    out = f"{_dir(slug)}/{slug}-{hook}-scaffold-v{version}.mp4"
    r = await media.render_timeline(ctx.run_id, out, _with_tags(p.timeline))
    sheet = await _cut_sheet(out, p.report)  # the agent sees what the human is watching
    price = sp.total_usd(p.generate)
    shots = ", ".join(f"{g['beat']} ({g['seconds']}s)" for g in p.generate)
    pc = p.report["pace"]
    fmt = script.get("format")
    question = (f"Scaffold v{version} of \"{script['title']}\" is ready to watch in Files: {out} "
                f"({r['duration_s']:.1f}s, free so far; beats are labelled b1, b2… in the corner). "
                + (f"Format: {fmt['name']}, from {fmt['examples']} real video(s). " if fmt else "No format card. ")
                + (f"Approving it means paying about ${price:.2f} to generate {len(p.generate)} AI shot(s): {shots}. "
                   if p.generate else "It's all real footage: nothing to generate. ")
                + f"Pace now: voice {pc['voice_speed']}×, {pc['beat_gap_s']}s between lines, at least "
                  f"{pc['min_shot_s']}s a shot. Approve, or tell me what to change (pace, a line, a shot, the hook).")
    options = ["Looks good: generate it" if p.generate else "Looks good", "Slower", "Faster"]
    answer = (await ctx.ask_human(question, agent=get_agent_id(), data={"options": options, "file": out})).strip()
    approved = answer.lower().startswith("looks good")
    applied = next((k for k in ("slower", "faster") if answer.lower() == k), None)
    async with _lock(slug):
        script = await _load(slug)
        if applied:
            script["pace"] = sp.pace(script, preset=applied)
        script.setdefault("reviews", []).append({
            "version": version, "hook": hook, "video": out, "duration_s": r["duration_s"], "price_usd": price,
            "pace": pc, "answer": answer, "approved": approved,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        await _write_json(f"{_dir(slug)}/script.json", script)
    _emit(f"Scaffold v{version} of {slug}: {'approved' if approved else 'changes asked'}",
          {"slug": slug, "version": version, "video": out, "answer": answer[:300]})
    if approved:
        nxt = ("short_voiceover(provider=\"elevenlabs\"), short_plan, then generate the AI shots" if p.generate
               else "short_voiceover(provider=\"elevenlabs\"), short_plan, short_render(mode=\"final\")")
    elif applied:
        nxt = f"applied \"{applied}\": short_voiceover (the voice speed changed), short_plan, short_review again"
    else:
        nxt = "act on the feedback (short_edit, short_pace, short_record), voice, plan, then short_review again"
    return {"approved": approved, "feedback": answer, "version": version, "video": out, "sheet": sheet,
            "applied": applied, "pace": script["pace"], "next": nxt}


SHORTS_TOOLS = [short_look, short_record, short_new, short_edit, short_pace, short_voices, short_voiceover, short_plan,
                short_render, short_review]
