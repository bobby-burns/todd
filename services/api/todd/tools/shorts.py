"""The `shorts` toolset: short vertical videos with a voiceover, timed from the voice.

Order (docs/video-step2-plan.md, "Locking voice to picture"):
  short_new / short_edit   the script: hook variants and beats, one voiceover line, on-screen text and shot each
  short_voiceover          the voice lock: one take per hook variant, with character timestamps (ElevenLabs)
  short_plan               every cut, caption, hold and clip length from the take, a sync report, the generation price
  short_render             the animatic (AI shots as labelled start frames) or the final cut

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
from typing import Any

from .. import shorts_plan as sp
from .. import vault
from ..policy import SpendDenied, authorize_spend, settle
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from . import elevenlabs, media, sandbox
from .sandbox_tools import _abs, _root
from .video import IMAGE_EXT, _rel, _slugify

SOURCES = ("screen", "clip", "image", "ai")
VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")
MAX_HOOKS, MAX_BEATS = 3, 10
MAX_VO_CHARS, MAX_TEXT_CHARS, MAX_PROMPT = 160, 60, 600
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
PART_ID = re.compile(r"(h|b)[0-9]{1,2}")
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
    lo, hi = rules["ideal_s"]
    return [] if lo <= est <= hi else [f"about {est:.0f}s: outside the {lo}–{hi}s sweet spot"]


@todd_tool(toolset="shorts")
async def short_new(title: str, hooks: list[dict], beats: list[dict], platform: str = "tiktok",
                    voice_id: str | None = None, voice_model: str | None = None, end_hold_s: float = 0.6) -> dict:
    """Start a short: writes its script to video/<slug>/script.json. Write it the way people in the niche talk.

    Args:
        title: what it is, e.g. "drop-in roster skit"
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
              "beats": [_part(b, f"b{i}") for i, b in enumerate(beats, 1)], "takes": {}}
    warnings = _check_length(script)
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
    try:
        return sp.take_text(script, hook)[0] != take.get("text")
    except ValueError:
        return True


# ------------------------------------------------------------------------------------------ the voice
def _key() -> str:
    key = vault.get_secret("ELEVENLABS_API_KEY")
    if not key:
        raise ToolError(elevenlabs.NO_KEY)
    return key


@todd_tool(toolset="shorts")
async def short_voices(search: str | None = None) -> dict:
    """List the ElevenLabs voices this account can use, to pick one for a short.

    Args:
        search: words to match against name, description and labels, e.g. "young male casual"
    """
    vs = await elevenlabs.voices(_key(), search)
    return {"voices": vs[:25], "count": len(vs)}


@todd_tool(toolset="shorts")
async def short_voiceover(slug: str, hooks: list[str] | None = None, voice_id: str | None = None) -> dict:
    """Voice the short: one continuous take per hook variant (hook + beats), with character timestamps. The take is the
    master clock: cuts, captions and clip lengths are planned from it (short_plan). Costs cents; one charge for all
    takes.

    Args:
        slug: the short, from short_new
        hooks: which hook variants to voice (default all)
        voice_id: an ElevenLabs voice (see short_voices); default the script's, else a premade narration voice
    """
    key = _key()
    ctx = get_ctx()
    script = await _load(slug)
    ids = [h["id"] for h in script["hooks"]]
    todo = hooks or ids
    unknown = [h for h in todo if h not in ids]
    if unknown:
        raise ToolError(f"no hook {', '.join(unknown)}: hooks are {', '.join(ids)}")
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
            r = await elevenlabs.speak(key, voice, texts[h], model)
            audio = f"{_dir(slug)}/audio/vo-{h}.mp3"
            await media.put(ctx.run_id, audio, r["audio"])
            take = {"text": texts[h], "voice_id": voice, "model_id": model, "chars": len(texts[h]),
                    "request_id": r.get("request_id"), "audio": audio, "alignment": r["alignment"]}
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
            takes[h] = {"audio": d["audio"], "timestamps": d["timestamps"], "text": texts[h]}
        await _write_json(f"{_dir(slug)}/script.json", script)
    _emit(f"Voiced {slug}: {', '.join(done)}", {"slug": slug, "takes": done, "usd": usd})
    return {"takes": done, "voice_id": voice, "usd": usd, "next": f"short_plan(\"{slug}\")"}


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
    r = await media.render_timeline(get_ctx().run_id, out, p.timeline)
    result = {"video": r["path"], "duration_s": r["duration_s"], "mode": mode, "warnings": p.report["warnings"],
              "generate": p.generate, "generate_usd": sp.total_usd(p.generate)}
    _emit(f"Rendered {mode} {slug} {hook}: {r['duration_s']:g}s", {"slug": slug, "video": r["path"]})
    return result


SHORTS_TOOLS = [short_new, short_edit, short_voices, short_voiceover, short_plan, short_render]
