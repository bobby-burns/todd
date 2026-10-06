"""The timing plan for a short: the voiceover take is the master clock, and everything else is derived from it.

Pure functions, no I/O. In: the script (script.json), one take's character timestamps (ElevenLabs `alignment`), and
what the shots' files are (durations, from the media service). Out: a timeline the media service renders
(/render/timeline), a sync report, and the generation spec (length, model, price) for every AI shot not generated yet.

Rules (docs/video-step2-plan.md, "Locking voice to picture"):
  * the hook starts at frame 0 with the take's leading silence trimmed (PRE_ROLL kept);
  * each later beat cuts LEAD_FRAMES before its first word, on a frame boundary;
  * `hold_s` on a beat adds silence after its line: the take is split in the pause between lines, never stretched;
  * `sync: {word, at_s}` on a shot puts the clip's moment `at_s` on that word;
  * captions come from the same timestamps, 1–4 words a page (pace.caption_words), never across a beat;
  * pace (the human's feedback on a scaffold, e.g. "slower"): a pause after every line (beat_gap_s) and a minimum time
    on screen for every shot (min_shot_s, reached by holding after a short line); voice_speed is applied when voicing;
  * AI shots get the shortest length the model supports that covers the slot plus HANDLE_S.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from typing import Any

FPS = 30
LEAD_FRAMES = 2  # the picture changes this many frames before the voice, as editors cut
PRE_ROLL = 0.05  # seconds of the take kept before the first word
TAIL = 0.25  # seconds of the take kept after a beat's last word when a hold follows
HANDLE_S = 0.25  # extra length asked of an AI clip beyond its slot
MIN_BEAT_S = 0.7
MAX_SPEEDUP = 2.0  # a recording may be sped up this much to fit its slot ("speed": "fit")
MIN_SPEED = 0.85  # and slowed down this little
MAX_FREEZE_S = 0.5  # holding a clip's last frame longer than this is flagged
WORDS_PER_S = 2.8  # for estimating a script's length before it's voiced
MIN_PAGE_S = 0.25  # a caption page shown shorter than this can't be read
PAGE_WORDS, PAGE_CHARS = 3, 18

# How fast a short moves. A person tunes it from the scaffold ("slower", "faster", or numbers); defaults are calm enough
# to follow on a first watch.
DEFAULT_PACE = {"voice_speed": 1.0, "beat_gap_s": 0.25, "caption_words": 3, "min_shot_s": 1.4}
PACE_LIMITS = {"voice_speed": (0.8, 1.2), "beat_gap_s": (0.0, 1.5), "caption_words": (1, 4), "min_shot_s": (0.6, 4.0)}
PRESETS = {"slower": {"voice_speed": -0.08, "beat_gap_s": 0.2, "min_shot_s": 0.4, "caption_words": -1},
           "faster": {"voice_speed": 0.08, "beat_gap_s": -0.15, "min_shot_s": -0.3, "caption_words": 1}}


def pace(script: dict[str, Any], change: dict[str, Any] | None = None, preset: str | None = None) -> dict[str, Any]:
    """The script's pace, with a preset or explicit values applied, clamped to sane limits."""
    out = {**DEFAULT_PACE, **(script.get("pace") or {})}
    for k, d in (PRESETS.get(preset or "") or {}).items():
        out[k] = out[k] + d
    for k, v in (change or {}).items():
        if v is not None and k in out:
            out[k] = v
    for k, (lo, hi) in PACE_LIMITS.items():
        out[k] = min(max(float(out[k]), lo), hi)
    out["caption_words"] = int(round(out["caption_words"]))
    out["voice_speed"] = round(out["voice_speed"], 2)
    return out

# Platform rules as data: max length, the sweet spot, and safe zones (shares of the height) for text.
PLATFORMS: dict[str, dict[str, Any]] = {
    "tiktok": {"max_s": 60, "ideal_s": (7, 20), "safe": (0.15, 0.70)},
    "reels": {"max_s": 90, "ideal_s": (7, 20), "safe": (0.14, 0.68)},
    "shorts": {"max_s": 60, "ideal_s": (7, 25), "safe": (0.12, 0.72)},
}

# Higgsfield models: lengths each accepts and price per second of output (https://higgsfield.ai/blog/higgsfield-api,
# 2026-10-06). Verify the lengths against the model pages on docs.higgsfield.ai before generating for real.
AI_MODELS: dict[str, dict[str, Any]] = {
    "seedance-2.5": {"name": "Seedance 2.5", "durations": list(range(2, 13)), "usd_per_s": 0.0738},
    "kling-3.0": {"name": "Kling 3.0", "durations": [5, 10], "usd_per_s": 0.112},
    "pixverse-6": {"name": "PixVerse 6", "durations": [5, 8], "usd_per_s": 0.115},
}
DEFAULT_AI_MODEL = "seedance-2.5"


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")


def plain(text: str) -> str:
    """On-screen text without emoji: the render font can't draw them (they'd show as empty boxes)."""
    return " ".join(_EMOJI.sub("", text).split())


def frames(t: float, fps: int = FPS) -> int:
    return int(round(t * fps))


@dataclass
class Word:
    text: str
    start: float  # seconds into the take
    end: float
    beat: int  # index into the take's parts (0 = the hook)


@dataclass
class Plan:
    timeline: dict[str, Any]
    report: dict[str, Any]
    generate: list[dict[str, Any]] = field(default_factory=list)


# ------------------------------------------------------------------------------------------ the take
def parts(script: dict[str, Any], hook_id: str) -> list[dict[str, Any]]:
    """The hook variant followed by the beats: what one take says, in order."""
    hook = next((h for h in script["hooks"] if h["id"] == hook_id), None)
    if hook is None:
        raise ValueError(f"no hook {hook_id!r}")
    return [hook, *script["beats"]]


def take_text(script: dict[str, Any], hook_id: str) -> tuple[str, list[tuple[int, int]]]:
    """The text sent for one take, and each part's (start, end) character span in it."""
    text, spans = "", []
    for p in parts(script, hook_id):
        line = " ".join(str(p["vo"]).split())
        if text:
            text += " "
        spans.append((len(text), len(text) + len(line)))
        text += line
    return text, spans


def words(text: str, spans: list[tuple[int, int]], alignment: dict[str, Any]) -> list[Word]:
    """Words with times, from the take's character alignment, each tagged with its part. The alignment's characters are
    matched back to the text sent (they can differ slightly, e.g. normalised spaces), so every word knows its beat."""
    chars = list(alignment.get("characters") or [])
    starts = list(alignment.get("character_start_times_seconds") or [])
    ends = list(alignment.get("character_end_times_seconds") or [])
    if not chars or not (len(chars) == len(starts) == len(ends)):
        raise ValueError("the take has no usable alignment")
    to_text: dict[int, int] = {}  # alignment index -> text index
    for blk in difflib.SequenceMatcher(None, text, "".join(chars), autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            to_text[blk.b + k] = blk.a + k

    def part_of(text_idx: int) -> int:  # the last part starting at or before this character
        return next((k for k in range(len(spans) - 1, -1, -1) if text_idx >= spans[k][0]), 0)

    out: list[Word] = []
    cur: list[int] = []
    for i, ch in enumerate([*chars, " "]):
        if ch.strip():
            cur.append(i)
            continue
        if cur:
            idx = next((to_text[j] for j in cur if j in to_text), None)
            beat = part_of(idx) if idx is not None else (out[-1].beat if out else 0)
            out.append(Word("".join(chars[j] for j in cur), float(starts[cur[0]]), float(ends[cur[-1]]), beat))
            cur = []
    return out


# ------------------------------------------------------------------------------------------ planning
def choose_length(model: str, needed_s: float) -> int | None:
    lengths = AI_MODELS[model]["durations"]
    return next((d for d in lengths if d >= needed_s - 1e-9), None)


def estimate_seconds(script: dict[str, Any]) -> float:
    """Before a take exists: roughly how long the script runs."""
    longest_hook = max(len(str(h["vo"]).split()) for h in script["hooks"])
    body = sum(len(str(b["vo"]).split()) for b in script["beats"])
    holds = sum(float(b.get("hold_s") or 0) for b in [*script["hooks"], *script["beats"]])
    return (longest_hook + body) / WORDS_PER_S + holds + float(script.get("end_hold_s", 0.6))


def _caption_text(w: str) -> str:
    return w.rstrip(",.;:—–-") or w


def plan(script: dict[str, Any], hook_id: str, alignment: dict[str, Any], media: dict[str, dict[str, Any]],
         take_audio: str, fps: int = FPS) -> Plan:
    """Plan one cut (one hook variant). `media` maps file paths to {kind, duration_s} for the shots' files."""
    text, spans = take_text(script, hook_id)
    ws = words(text, spans, alignment)
    ps = parts(script, hook_id)
    n = len(ps)
    by_part: list[list[Word]] = [[w for w in ws if w.beat == k] for k in range(n)]
    missing = [ps[k]["id"] for k in range(n) if not by_part[k]]
    if missing:
        raise ValueError(f"no words found for {', '.join(missing)}: voice the script again")
    pc = pace(script)
    end_hold = float(script.get("end_hold_s", 0.6))
    lead_trim = max(0.0, by_part[0][0].start - PRE_ROLL)
    # pauses: the part's own hold, a gap after every line but the last, and whatever lifts a short shot to min_shot_s
    holds = [float(p.get("hold_s") or 0) + (pc["beat_gap_s"] if k < n - 1 else 0.0) for k, p in enumerate(ps)]
    lead = LEAD_FRAMES / fps  # cuts land this early, so the first shot loses it and the last one gains it
    for k in range(n):
        start = lead_trim if k == 0 else by_part[k][0].start - lead
        end = by_part[k + 1][0].start - lead if k + 1 < n else by_part[k][-1].end + end_hold
        natural = end - start
        if natural + holds[k] < pc["min_shot_s"] + 0.5 / fps:
            holds[k] = pc["min_shot_s"] + 0.5 / fps - natural

    # the take is split in the pause before each part; holds push later parts back
    splits = [lead_trim]
    for k in range(1, n):
        prev_end, nxt = by_part[k - 1][-1].end, by_part[k][0].start
        splits.append(prev_end + max(0.0, nxt - prev_end) / 2)
    take_end = by_part[-1][-1].end + TAIL
    shift = [sum(holds[:k]) for k in range(n)]  # added silence before part k

    def tl(t_take: float, k: int) -> float:  # take time -> timeline time, for something in part k
        return t_take - lead_trim + shift[k]

    segments = []
    for k in range(n):
        src_in, src_out = splits[k], (splits[k + 1] if k + 1 < n else take_end)
        segments.append({"src_in": round(src_in, 4), "src_out": round(src_out, 4), "at": round(tl(src_in, k), 4)})

    total_f = frames(tl(by_part[-1][-1].end, n - 1) + holds[-1] + end_hold, fps)
    cuts = [0]
    for k in range(1, n):
        cuts.append(max(cuts[-1] + 1, frames(tl(by_part[k][0].start, k), fps) - LEAD_FRAMES))
    cuts.append(max(total_f, cuts[-1] + 1))

    platform = PLATFORMS.get(script.get("platform", "tiktok"), PLATFORMS["tiktok"])
    warnings: list[str] = []
    beats_report: list[dict[str, Any]] = []
    video: list[dict[str, Any]] = []
    generate: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    overlays: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []  # beat labels for the scaffold, so feedback can say "b3 is rushed"

    for k, p in enumerate(ps):
        start_f, end_f = cuts[k], cuts[k + 1]
        slot = (end_f - start_f) / fps
        start_s, end_s = start_f / fps, end_f / fps
        shot = p.get("shot") or {}
        notes: list[str] = []
        item, fit = _fit_shot(p["id"], shot, slot, start_s, by_part[k], tl, k, media, notes)
        item["frames"] = end_f - start_f
        video.append(item)
        if item["kind"] == "placeholder":
            spec = _generation(p["id"], shot, slot)
            generate.append(spec)
            notes += spec.pop("notes")
            item["label"] = (f"{AI_MODELS[spec['model']]['name']} · {spec['seconds']} s · ${spec['usd']:.2f} · "
                             f"{shot.get('prompt', '')}")[:400]
        if slot < min(MIN_BEAT_S, pc["min_shot_s"]) - 1e-6:
            notes.append(f"only {slot:.2f}s on screen (under {min(MIN_BEAT_S, pc['min_shot_s'])}s)")
        tags.append({"text": f"{p['id']} · {slot:.1f}s", "start": round(start_s, 3), "end": round(end_s, 3),
                     "position": "tag"})
        if p.get("text"):
            text = plain(p["text"])
            if text != p["text"]:
                notes.append("emoji left out of the on-screen text (the font can't draw them): add them in the app "
                             "when posting")
            if text:
                overlays.append({"text": text, "start": round(start_s, 3), "end": round(end_s, 3), "position": "top"})
        pages = _pages(by_part[k], tl, k, end_s, pc["caption_words"])
        for pg in pages:
            if pg["end"] - pg["start"] < MIN_PAGE_S:
                notes.append(f"caption \"{' '.join(w['text'] for w in pg['words'])}\" is on screen only "
                             f"{pg['end'] - pg['start']:.2f}s")
        captions += pages
        beats_report.append({"id": p["id"], "vo": p["vo"], "start_s": round(start_s, 3), "end_s": round(end_s, 3),
                             "slot_s": round(slot, 3), "shot": shot.get("source"), "fit": fit, "warnings": notes})
        warnings += [f"{p['id']}: {x}" for x in notes]

    duration = total_f / fps
    lo, hi = platform["ideal_s"]
    if duration > platform["max_s"]:
        warnings.append(f"{duration:.1f}s is over the platform's {platform['max_s']}s limit")
    elif not lo <= duration <= hi:
        warnings.append(f"{duration:.1f}s is outside the {lo}–{hi}s sweet spot")
    music = script.get("music")
    timeline = {
        "version": 1, "slug": script["slug"], "hook": hook_id, "fps": fps, "width": script["width"],
        "height": script["height"], "frames": total_f, "duration_s": round(duration, 3),
        "video": video, "voice": {"src": take_audio, "segments": segments},
        "music": {"src": music["path"], "volume": float(music.get("volume", 0.25))} if music else None,
        "captions": captions, "overlays": overlays, "tags": tags,
    }
    report = {"hook": hook_id, "duration_s": round(duration, 3), "beats": beats_report, "warnings": warnings,
              "estimate_usd": round(sum(g["usd"] for g in generate), 2), "pace": pc}
    return Plan(timeline, report, generate)


def _fit_shot(beat_id: str, shot: dict[str, Any], slot: float, start_s: float, ws: list[Word], tl, k: int,
              media: dict[str, dict[str, Any]], notes: list[str]) -> tuple[dict[str, Any], str]:
    """How a shot fills its slot: (timeline video item, a short description of the fit)."""
    source = shot.get("source")
    path = shot.get("clip") if source == "ai" else shot.get("path")
    if source == "ai" and not path:
        return {"kind": "placeholder", "src": shot.get("start_image"), "zoom": "in"}, "placeholder (not generated)"
    info = media.get(path or "") or {}
    if source == "image" or info.get("kind") == "image":
        return {"kind": "image", "src": path, "fit": shot.get("fit", "cover"), "zoom": shot.get("zoom", "in")}, "still"
    dur = info.get("duration_s")
    if not dur:
        notes.append(f"{path}: not a video the planner could read")
        return {"kind": "placeholder", "src": None, "label": f"missing: {path}"}, "missing"
    in_s = float(shot.get("in_s") or 0)
    sync = shot.get("sync") or {}
    speed_mode = shot.get("speed", 1)
    speed = 1.0 if speed_mode in (None, "fit") else float(speed_mode)
    if sync.get("word"):
        target = sync["word"].lower().strip(".,!?")
        hit = next((w for w in ws if w.text.lower().strip(".,!?\"'") == target), None)
        if hit is None:
            notes.append(f"sync word {sync['word']!r} isn't in this beat's line")
        else:
            offset = tl(hit.start, k) - start_s  # seconds into the slot where the word is spoken
            in_s = float(sync.get("at_s", 0)) - offset * speed
            if in_s < 0:
                notes.append(f"sync: the clip would have to start {-in_s:.2f}s before its beginning; starts at 0")
                in_s = 0.0
    available = max(0.0, dur - in_s)
    fit = "trim"
    if speed_mode == "fit" and available > slot:
        speed = min(available / slot, MAX_SPEEDUP)
        fit = f"sped up {speed:.2f}×"
        if available / slot > MAX_SPEEDUP:
            notes.append(f"recording is {available:.1f}s for a {slot:.1f}s slot: sped up {MAX_SPEEDUP}× and trimmed")
    covered = available / speed
    freeze = 0.0
    if covered < slot - 1e-6:  # short: a slight slow-down shows less than a freeze, a short freeze less than a gap
        short = slot - covered
        if available / slot >= MIN_SPEED:
            speed, fit = available / slot, f"slowed to {available / slot:.2f}×"
        elif short <= MAX_FREEZE_S:
            freeze, fit = short, f"holds its last frame {short:.2f}s"
        else:
            freeze, fit = short, f"too short by {short:.2f}s (holds its last frame)"
            notes.append(f"{path} is {short:.2f}s too short for its slot: re-record or re-generate it longer")
    item = {"kind": "clip", "src": path, "in_s": round(in_s, 3), "speed": round(speed, 4),
            "freeze_s": round(freeze, 3), "fit": shot.get("fit", "cover")}
    return item, fit


def _generation(beat_id: str, shot: dict[str, Any], slot: float) -> dict[str, Any]:
    model = shot.get("model") or DEFAULT_AI_MODEL
    notes: list[str] = []
    if model not in AI_MODELS:
        notes.append(f"unknown model {model!r}; priced as {DEFAULT_AI_MODEL}")
        model = DEFAULT_AI_MODEL
    needed = slot + HANDLE_S
    seconds = choose_length(model, needed)
    if seconds is None:
        seconds = AI_MODELS[model]["durations"][-1]
        notes.append(f"needs {needed:.1f}s but {AI_MODELS[model]['name']} makes at most {seconds}s: split the beat")
    usd = round(seconds * AI_MODELS[model]["usd_per_s"], 3)
    return {"beat": beat_id, "model": model, "seconds": seconds, "slot_s": round(slot, 3), "usd": usd,
            "prompt": shot.get("prompt", ""), "start_image": shot.get("start_image"), "notes": notes}


def _pages(ws: list[Word], tl, k: int, beat_end_s: float, per_page: int = PAGE_WORDS) -> list[dict[str, Any]]:
    """Caption pages for one beat: up to `per_page` words (and ~6 characters a word), breaking after punctuation."""
    pages: list[list[Word]] = []
    limit = max(PAGE_CHARS * per_page // PAGE_WORDS, 8)
    for w in ws:
        cur = pages[-1] if pages else None
        joined = " ".join(x.text for x in cur) if cur else ""
        if cur is None or len(cur) >= per_page or len(joined) + 1 + len(w.text) > limit \
                or re.search(r"[.!?;:,]$", cur[-1].text):
            pages.append([w])
        else:
            cur.append(w)
    out = []
    for i, pg in enumerate(pages):
        start = tl(pg[0].start, k)
        end = tl(pages[i + 1][0].start, k) if i + 1 < len(pages) else min(tl(pg[-1].end, k) + 0.3, beat_end_s)
        out.append({"start": round(start, 3), "end": round(max(end, start + 0.05), 3),
                    "words": [{"text": plain(_caption_text(w.text)) or "·", "start": round(tl(w.start, k), 3),
                               "end": round(tl(w.end, k), 3)} for w in pg]})
    return out


def merge_generation(plans: list[Plan]) -> list[dict[str, Any]]:
    """What to generate for all hook variants: one clip per distinct AI shot (same model, prompt and start frame, even
    in different hooks), long enough for the longest slot it fills. `beats` lists every hook and beat that uses it."""
    best: dict[tuple, dict[str, Any]] = {}
    for p in plans:
        for g in p.generate:
            key = (g["model"], g["prompt"], g.get("start_image"))
            cur = best.get(key)
            beats = sorted({*(cur["beats"] if cur else []), g["beat"]})
            if cur is None or g["slot_s"] > cur["slot_s"]:
                cur = {k: v for k, v in g.items() if k != "beat"}
            best[key] = {**cur, "beats": beats}
    return list(best.values())


def total_usd(specs: list[dict[str, Any]]) -> float:
    return round(math.fsum(g["usd"] for g in specs), 2)
