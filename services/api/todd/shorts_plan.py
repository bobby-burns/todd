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
MAX_STILL_S = 4.5  # one shot on screen longer than this, with nothing changing, is where people swipe
READ_WPS = 3.3  # on-screen words a viewer reads per second, for text-led beats
STOCK_DIR = "video/stock/"  # stock clips (short_stock): toned down to sit with phone footage
MAX_ITEMS = 120  # pieces of video one timeline render takes (the media service's limit)
MAX_SCREEN_HOLD_S = 3.0  # a screen recording is a still page at either end: holding it this long reads naturally
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

# The most a short should run, whatever the platform allows: a ceiling, not a target. Most land at 8–20 s.
CEILING_S = 30
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
    """The text sent for one take, and each spoken part's (start, end) character span in it (silent parts aren't in
    the take)."""
    text, spans = "", []
    for p in parts(script, hook_id):
        if not p.get("vo"):
            continue
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
    def length(p: dict[str, Any]) -> float:
        return len(str(p["vo"]).split()) / WORDS_PER_S if p.get("vo") else float(p.get("seconds") or 0)

    longest_hook = max(length(h) for h in script["hooks"])
    body = sum(length(b) for b in script["beats"])
    holds = sum(float(b.get("hold_s") or 0) for b in [*script["hooks"], *script["beats"]])
    voiced = any(p.get("vo") for p in [*script["hooks"], *script["beats"]])
    return longest_hook + body + holds + (float(script.get("end_hold_s", 0.6)) if voiced else 0.0)


def _caption_text(w: str) -> str:
    return w.rstrip(",.;:—–-") or w


def length_notes(seconds: float, platform: dict[str, Any], about: str = "") -> list[str]:
    """What's wrong with a short's length, if anything: past the ceiling is a cut to make, not a style."""
    lo, hi = platform["ideal_s"]
    if seconds > min(platform["max_s"], CEILING_S):
        return [f"{about}{seconds:.0f}s is too long: cut lines until it's under {CEILING_S}s ({CEILING_S}s is a "
                f"ceiling, not a target; most shorts land at {lo + 1}–{hi}s, and a real example's length isn't one "
                "to match)"]
    if seconds > hi:
        return [f"{about}{seconds:.0f}s is past the {lo}–{hi}s sweet spot: keep only the lines that earn their place"]
    if seconds < lo:
        return [f"{about}{seconds:.0f}s is under {lo}s: it ends before it lands"]
    return []


def _shots_of(p: dict[str, Any]) -> list[dict[str, Any]]:
    """A part's shots: one, or a few quick cuts inside it (`shots`)."""
    return list(p.get("shots") or [p.get("shot") or {}])


def _split(frames_total: int, shots: list[dict[str, Any]]) -> list[int]:
    """Frames for each cut inside a part, by their `share` (equal by default); every cut gets at least one frame."""
    if frames_total < len(shots):
        raise ValueError(f"{len(shots)} cuts don't fit in {frames_total} frames: fewer cuts, or a longer beat")
    if len(shots) == 1:
        return [frames_total]
    weights = [max(float(sh.get("share") or 1), 0.05) for sh in shots]
    total_w = sum(weights)
    out, used = [], 0
    for i, wt in enumerate(weights):
        left = len(weights) - i - 1
        f = frames_total - used - left if i == len(weights) - 1 else \
            min(max(1, round(frames_total * wt / total_w)), frames_total - used - left)
        out.append(f)
        used += f
    return out


def plan(script: dict[str, Any], hook_id: str, alignment: dict[str, Any] | None, media: dict[str, dict[str, Any]],
         take_audio: str | None, fps: int = FPS) -> Plan:
    """Plan one cut (one hook variant). `media` maps file paths to {kind, duration_s} for the shots' files. Parts with a
    spoken line (`vo`) are timed from the take; parts without one are silent and last their `seconds` (a text-led beat,
    a visual payoff). A short with no spoken line at all needs no take."""
    ps = parts(script, hook_id)
    n = len(ps)
    voiced = [k for k, p in enumerate(ps) if p.get("vo")]
    silent = [0.0 if p.get("vo") else float(p.get("seconds") or 0) for p in ps]
    for k, p in enumerate(ps):
        if not p.get("vo") and silent[k] <= 0:
            raise ValueError(f"{p['id']} has no spoken line, so it needs seconds")
    pc = pace(script)
    end_hold = float(script.get("end_hold_s", 0.6))
    lead = LEAD_FRAMES / fps  # cuts land this early, so the first shot loses it and the last one gains it
    by_part: list[list[Word]] = [[] for _ in range(n)]
    segments: list[dict[str, Any]] = []
    start_f = [0] * n
    own_end_f = [0] * n  # where a voiced part's own picture ends (its trailing silent parts follow)
    tl = None
    if voiced:
        if alignment is None:
            raise ValueError("the short has spoken lines but no take: voice it first")
        text, spans = take_text(script, hook_id)
        ws = words(text, spans, alignment)
        for w in ws:
            w.beat = voiced[w.beat] if w.beat < len(voiced) else voiced[-1]
            by_part[w.beat].append(w)
        missing = [ps[k]["id"] for k in voiced if not by_part[k]]
        if missing:
            raise ValueError(f"no words found for {', '.join(missing)}: voice the script again")
        nxt = {j: (voiced[i + 1] if i + 1 < len(voiced) else n) for i, j in enumerate(voiced)}
        trail = {j: list(range(j + 1, nxt[j])) for j in voiced}  # the silent parts after each voiced one
        lead_s = sum(silent[:voiced[0]])  # silent parts before the first line
        first, last = voiced[0], voiced[-1]
        lead_trim = max(0.0, by_part[first][0].start - PRE_ROLL)
        own: dict[int, float] = {}
        holds: dict[int, float] = {}
        for i, j in enumerate(voiced):
            is_last = j == last
            h = float(ps[j].get("hold_s") or 0) + (pc["beat_gap_s"] if not is_last else 0.0)
            begin = lead_trim if j == first else by_part[j][0].start - lead
            if is_last:
                finish = by_part[j][-1].end + (end_hold if not trail[j] else TAIL)
            else:
                finish = by_part[nxt[j]][0].start - lead
            if finish - begin + h < pc["min_shot_s"] + 0.5 / fps:
                h = pc["min_shot_s"] + 0.5 / fps - (finish - begin)
            own[j] = h
            holds[j] = h + sum(silent[k] for k in trail[j])
        shift: dict[int, float] = {}
        acc = lead_s
        for j in voiced:
            shift[j] = acc
            acc += holds[j]

        def tl(t_take: float, k: int) -> float:  # take time -> timeline time, for something in voiced part k
            return t_take - lead_trim + shift[k]

        # the take is split in the pause before each line; holds and silent parts push later lines back
        splits = [lead_trim]
        for i in range(1, len(voiced)):
            prev_end, nx = by_part[voiced[i - 1]][-1].end, by_part[voiced[i]][0].start
            splits.append(prev_end + max(0.0, nx - prev_end) / 2)
        take_end = by_part[last][-1].end + TAIL
        for i, j in enumerate(voiced):
            src_in, src_out = splits[i], (splits[i + 1] if i + 1 < len(voiced) else take_end)
            segments.append({"src_in": round(src_in, 4), "src_out": round(src_out, 4), "at": round(tl(src_in, j), 4)})
        tail_end = (end_hold if not trail[last] else TAIL)
        total_f = frames(tl(by_part[last][-1].end, last) + own[last] + tail_end + sum(silent[k] for k in trail[last]),
                         fps)
        # leading silent parts, one after another from frame 0
        at = 0.0
        for k in range(first):
            start_f[k] = frames(at, fps)
            at += silent[k]
        cut = {}
        for i, j in enumerate(voiced):
            cut[j] = frames(lead_s, fps) if i == 0 else max(cut[voiced[i - 1]] + 1,
                                                            frames(tl(by_part[j][0].start, j), fps) - LEAD_FRAMES)
            start_f[j] = cut[j]
        for i, j in enumerate(voiced):
            end_j = cut[voiced[i + 1]] if i + 1 < len(voiced) else max(total_f, cut[j] + 1)
            for k in reversed(trail[j]):  # silent parts are carved from the end of the gap before the next line
                end_j -= max(1, frames(silent[k], fps))
                start_f[k] = end_j
            own_end_f[j] = max(end_j, cut[j] + 1)
            if end_j <= cut[j]:
                raise ValueError(f"{ps[j]['id']}: the silent parts after it don't fit")
    else:  # no voice at all: every part lasts its seconds
        at = 0.0
        for k in range(n):
            start_f[k] = frames(at, fps)
            at += silent[k]
        total_f = max(frames(at, fps), n)
    ends = [start_f[k + 1] if k + 1 < n else max(total_f, start_f[k] + 1) for k in range(n)]
    total_f = ends[-1]

    platform = PLATFORMS.get(script.get("platform", "tiktok"), PLATFORMS["tiktok"])
    style = script.get("text_style") or "box"
    warnings: list[str] = []
    beats_report: list[dict[str, Any]] = []
    video: list[dict[str, Any]] = []
    generate: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    overlays: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []  # beat labels for the scaffold, so feedback can say "b3 is rushed"
    caption_mode = script.get("captions") or "phrase"

    for k, p in enumerate(ps):
        start_f_k, end_f_k = start_f[k], ends[k]
        slot = (end_f_k - start_f_k) / fps
        start_s, end_s = start_f_k / fps, end_f_k / fps
        notes: list[str] = []
        shots = _shots_of(p)
        fits = []
        at_f = start_f_k
        for i, (shot, nf) in enumerate(zip(shots, _split(end_f_k - start_f_k, shots))):
            sub_id = p["id"] if len(shots) == 1 else f"{p['id']}.{i + 1}"
            sub_start = at_f / fps
            sub = nf / fps
            items, fit = _fit_shot(sub_id, shot, sub, sub_start, by_part[k], tl, k, media, notes, fps)
            for item in items:
                video.append(item)
            if items[0]["kind"] == "placeholder" and shot.get("source") == "ai":
                spec = _generation(sub_id, shot, sub)
                generate.append(spec)
                notes += spec.pop("notes")
                items[0]["label"] = (f"{AI_MODELS[spec['model']]['name']} · {spec['seconds']} s · ${spec['usd']:.2f} "
                                     f"· {shot.get('prompt', '')}")[:400]
            fits.append(fit if len(shots) == 1 else f"{sub_id}: {fit}")
            if len(shots) > 1 and sub < 0.5 - 1e-6:
                notes.append(f"{sub_id} is only {sub:.2f}s on screen: under half a second doesn't register")
            if sub > MAX_STILL_S and not (not p.get("vo") and len(ps) <= 2):  # a one-idea loop may hold
                notes.append(f"{sub_id} stays on one shot for {sub:.1f}s: cut it up (more shots, a punch-in with "
                             "focus) so something changes every 2–3 s")
            sub_text = shot.get("text")
            if sub_text:
                _overlay(overlays, notes, sub_text, sub_start, (at_f + nf) / fps, shot.get("text_position") or
                         p.get("text_position") or "top", shot.get("text_style") or p.get("text_style") or style)
            at_f += nf
        if slot < min(MIN_BEAT_S, pc["min_shot_s"]) - 1e-6:
            notes.append(f"only {slot:.2f}s on screen (under {min(MIN_BEAT_S, pc['min_shot_s'])}s)")
        tags.append({"text": f"{p['id']} · {slot:.1f}s", "start": round(start_s, 3), "end": round(end_s, 3),
                     "position": "tag"})
        if p.get("text") and not any(sh.get("text") for sh in shots):
            _overlay(overlays, notes, p["text"], start_s, end_s, p.get("text_position") or "top",
                     p.get("text_style") or style)
        elif p.get("text"):  # cuts with their own text: the part's text fills the cuts without one
            at_f = start_f_k
            for shot, nf in zip(shots, _split(end_f_k - start_f_k, shots)):
                if not shot.get("text"):
                    _overlay(overlays, notes, p["text"], at_f / fps, (at_f + nf) / fps,
                             p.get("text_position") or "top", p.get("text_style") or style)
                at_f += nf
        if p.get("vo") and caption_mode != "none":
            if "low" in (p.get("text_position"), *(sh.get("text_position") for sh in shots)):
                notes.append("text at \"low\" sits where the captions are: use top or middle on a spoken beat")
            own_end_s = own_end_f[k] / fps
            pages = _pages(by_part[k], tl, k, own_end_s, pc["caption_words"])
            for pg in pages:
                if pg["end"] - pg["start"] < MIN_PAGE_S:
                    notes.append(f"caption \"{' '.join(w['text'] for w in pg['words'])}\" is on screen only "
                                 f"{pg['end'] - pg['start']:.2f}s")
            captions += pages
        if not p.get("vo") and p.get("text"):
            need = len(str(p["text"]).split()) / READ_WPS + 0.4
            if slot < need - 0.05:
                notes.append(f"{slot:.1f}s isn't enough to read \"{p['text']}\" (about {need:.1f}s)")
        beats_report.append({"id": p["id"], "vo": p.get("vo") or "", "start_s": round(start_s, 3),
                             "end_s": round(end_s, 3), "slot_s": round(slot, 3),
                             "shot": "+".join(str(sh.get("source")) for sh in shots), "fit": "; ".join(fits),
                             "warnings": notes})
        warnings += [f"{p['id']}: {x}" for x in notes]

    spoken_beats = [p for p in ps[1:] if p.get("vo")]
    if caption_mode != "none" and len(spoken_beats) >= 3 and \
            sum(1 for p in spoken_beats if p.get("text") or any(sh.get("text") for sh in _shots_of(p))) \
            > len(spoken_beats) / 2:
        warnings.append("on-screen text on most spoken beats competes with the captions: keep text to the hook and a "
                        "label or two")
    if len(video) > MAX_ITEMS:
        raise ValueError(f"{len(video)} pieces of video is more than one render takes ({MAX_ITEMS}): fewer cuts")
    duration = total_f / fps
    warnings += length_notes(duration, platform)
    music = script.get("music")
    timeline = {
        "version": 1, "slug": script["slug"], "hook": hook_id, "fps": fps, "width": script["width"],
        "height": script["height"], "frames": total_f, "duration_s": round(duration, 3),
        "video": video, "voice": {"src": take_audio, "segments": segments} if voiced else None,
        "music": {"src": music["path"], "volume": float(music.get("volume", 0.25))} if music else None,
        "captions": captions, "caption_style": "karaoke" if caption_mode == "karaoke" else "phrase",
        "overlays": overlays, "tags": tags,
    }
    report = {"hook": hook_id, "duration_s": round(duration, 3), "beats": beats_report, "warnings": warnings,
              "estimate_usd": round(sum(g["usd"] for g in generate), 2), "pace": pc,
              "cuts": len(video) - 1, "avg_shot_s": round(duration / max(len(video), 1), 2)}
    return Plan(timeline, report, generate)


def _overlay(overlays: list[dict[str, Any]], notes: list[str], raw: str, start: float, end: float, position: str,
             style: str) -> None:
    text = plain(raw)
    if text != raw:
        notes.append("emoji left out of the on-screen text (the font can't draw them): add them in the app when "
                     "posting")
    if text:
        overlays.append({"text": text, "start": round(start, 3), "end": round(end, 3), "position": position,
                         "style": style})


def _fit_shot(beat_id: str, shot: dict[str, Any], slot: float, start_s: float, ws: list[Word], tl, k: int,
              media: dict[str, dict[str, Any]], notes: list[str], fps: int = FPS) -> tuple[list[dict[str, Any]], str]:
    """How a shot fills its slot: (timeline video items, a short description of the fit). One item, or two when a
    punch-in (`focus` with `at_s`) lands partway through."""
    frames_n = max(1, round(slot * fps))
    source = shot.get("source")
    path = shot.get("clip") if source == "ai" else shot.get("path")
    focus = shot.get("focus")
    crop = {k2: float(focus[k2]) for k2 in ("x", "y", "zoom")} if focus else None
    if source == "ai" and not path:
        return [{"kind": "placeholder", "src": shot.get("start_image"), "zoom": "in", "frames": frames_n}], \
            "placeholder (not generated)"
    info = media.get(path or "") or {}
    if source == "image" or info.get("kind") == "image":
        item = {"kind": "image", "src": path, "fit": shot.get("fit", "cover"), "zoom": shot.get("zoom", "in"),
                "frames": frames_n}
        if crop:
            item["crop"] = crop
        return [item], "still"
    dur = info.get("duration_s")
    if not dur:
        notes.append(f"{path}: not a video the planner could read")
        return [{"kind": "placeholder", "src": None, "label": f"missing: {path}", "frames": frames_n}], "missing"
    in_s = float(shot.get("in_s") or 0)
    sync = shot.get("sync") or {}
    speed_mode = shot.get("speed", 1)
    speed = 1.0 if speed_mode in (None, "fit") else float(speed_mode)
    # Screen recordings start and end on a still page, so holding a first or last frame looks like the page waiting;
    # a generated or filmed clip frozen that long looks broken.
    max_hold = MAX_SCREEN_HOLD_S if source == "screen" else MAX_FREEZE_S
    hold = 0.0  # the first frame, held before the clip plays
    if sync.get("word"):
        target = sync["word"].lower().strip(".,!?")
        hit = next((w for w in ws if w.text.lower().strip(".,!?\"'") == target), None)
        if hit is None or tl is None:
            notes.append(f"sync word {sync['word']!r} isn't in this beat's line")
        else:
            offset = tl(hit.start, k) - start_s  # seconds into the slot where the word is spoken
            if not 0 <= offset <= slot:
                notes.append(f"{beat_id}: sync word {sync['word']!r} is said while another cut is on screen")
            in_s = float(sync.get("at_s", 0)) - offset * speed
            if in_s < 0 and source == "screen" and -in_s <= MAX_SCREEN_HOLD_S:
                hold = min(-in_s, slot)
                in_s = 0.0
            elif in_s < 0:
                notes.append(f"sync: the clip would have to start {-in_s:.2f}s before its beginning; starts at 0")
                in_s = 0.0
    room = slot - hold  # what the clip itself has to fill
    available = max(0.0, dur - in_s)
    fit = f"holds its first frame {hold:.2f}s, then plays" if hold else "trim"
    if speed_mode == "fit" and available > room:
        speed = min(available / room, MAX_SPEEDUP)
        fit = f"sped up {speed:.2f}×"
        if available / room > MAX_SPEEDUP:
            notes.append(f"recording is {available:.1f}s for a {room:.1f}s slot: sped up {MAX_SPEEDUP}× and trimmed")
    covered = available / speed
    freeze = 0.0
    if covered < room - 1e-6:  # short: a slight slow-down shows less than a freeze, a short freeze less than a gap
        short = room - covered
        if available / room >= MIN_SPEED and not hold:
            speed, fit = available / room, f"slowed to {available / room:.2f}×"
        elif short + hold <= max_hold:
            freeze, fit = short, (f"{fit}; " if hold else "") + f"holds its last frame {short:.2f}s"
        else:
            freeze, fit = short, f"too short by {short:.2f}s (holds its last frame)"
            notes.append(f"{path} is {short:.2f}s too short for its slot: re-record or re-generate it longer")
    item = {"kind": "clip", "src": path, "in_s": round(in_s, 3), "speed": round(speed, 4),
            "freeze_s": round(freeze, 3), "fit": shot.get("fit", "cover"), "frames": frames_n}
    grade = shot.get("grade") or ("phone" if source == "ai" or str(path).startswith(STOCK_DIR) else None)
    if grade == "phone":
        item["grade"] = "phone"
    if hold:
        item["hold_s"] = round(hold, 3)
    if not crop:
        return [item], fit
    at = focus.get("at_s")
    if at is None:
        return [{**item, "crop": crop}], f"{fit}; punched in {crop['zoom']:g}×"
    if float(at) >= in_s + available - 1 / fps:
        notes.append(f"{beat_id}: the punch-in at {at}s is past the end of {path} ({dur:g}s): punched in from the start")
        return [{**item, "crop": crop}], f"{fit}; punched in {crop['zoom']:g}×"
    t_on = hold + (float(at) - in_s) / speed  # when the clip reaches at_s, seconds into the slot
    f1 = round(t_on * fps)
    if f1 <= 0:
        return [{**item, "crop": crop}], f"{fit}; punched in {crop['zoom']:g}×"
    if f1 >= frames_n:
        notes.append(f"{beat_id}: the punch-in at {at}s comes after this cut ends")
        return [item], fit
    first = {**item, "frames": f1, "freeze_s": 0.0}
    second = {**item, "frames": frames_n - f1, "in_s": round(max(float(at), in_s), 3), "crop": crop}
    second.pop("hold_s", None)
    return [first, second], f"{fit}; punches in {crop['zoom']:g}× at {t_on:.1f}s"


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
