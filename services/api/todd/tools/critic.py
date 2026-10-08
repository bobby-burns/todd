"""The critic: a separate agent watches a rendered short and judges it before the human sees it.

  short_critique   (shorts toolset, for the producer) renders the cut, has the media service watch it, and has a fresh
                   critic agent judge it: what's said and written against what's shown, beat by beat, the hook, how
                   organic it looks, the timing, the fit to its format card. Returns scores and concrete fixes.
  critic_watch     (critic toolset) the cut as the critic sees it: a filmstrip every half second, each frame labelled
                   with what is said and written at that moment; the beat-by-beat log (shots, script, what the
                   soundtrack actually says); what Todd measured; the format card and its real examples
  critic_verdict   (critic toolset) the critic's scores, notes, fixes and verdict

Iterating is free (the scaffold voice is local, stock and recordings cost nothing), so the producer applies the fixes
and asks again, inside guards that stop a loop: at most MAX_CRITIQUES per cut, only after something changed, never
after a pass, and a stop as soon as the score doesn't improve. The critic never edits; the producer does. A lesson the
critic draws for the format (not the product) is kept on the format card for later producers.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import re
import statistics
import time
from typing import Any

from .. import shorts_plan as sp
from ..config import config
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from . import media, sandbox

MAX_CRITIQUES = 3  # per cut (slug + hook): then it goes to the human whatever the score
CRITIC_TIMEOUT_S = 600
STRIP_FPS, STRIP_MAX = 2.0, 72  # a frame every half second, up to 36 s (longer cuts are sampled a little wider)
MAX_FIXES, MAX_LESSONS = 6, 10
SCORES = ("hook", "say_show", "organic", "timing", "format_fit")
VERDICTS = ("ship", "fix", "rethink")
HEARD_MATCH = 0.8  # below this, what the soundtrack says isn't what the script says

CRITIC_GUIDE = """\
You are the critic for a short vertical video (TikTok, Reels, Shorts) that another agent made for a product. You
didn't make it and don't defend it: judge what someone scrolling would see and hear.
1) Call critic_watch once (the slug, hook and version are in your task). You get: a filmstrip every half second, each
   frame labelled with the time, the beat, the words being said then and the text on screen; the beat-by-beat log
   (each shot, what the script says, what the soundtrack actually says); what Todd measured (frozen picture,
   silence, black frames); Todd's own checks; the format card it follows; sheets of the card's real examples when
   there are any. Look at every sheet.
2) Judge it beat by beat, then as a whole, scoring 1–5:
   - say_show (the most important): does what's said and written match what's on screen at that moment? A line
     about one thing while the screen shows another, a number or name said that isn't visible, text that
     contradicts the voice, a screen that's loading, cut off or unreadable when it matters, captions covering the
     screen text the line is about.
   - hook: would the first 1–2 s stop someone in this niche (readable text on the first frame, a reason to stay)?
   - organic: would people believe a person filmed and cut this on their phone? Template tells: every beat the same
     length and layout, a text box on every beat, stock that looks like stock (posed, glossy, slow motion, the wrong
     place), robotic screen moves, an ad's pitch or call to action, the product named over and over.
   - timing: shots too long or too short, frozen pictures, text gone before it can be read, the payoff late, dead
     air, an ending that doesn't loop.
   - format_fit: does it follow the card's shape, audio and pace (compare with the examples' measured pace and
     sheets)?
   Don't mark down how the free placeholder voice sounds, or AI shots shown as labelled placeholder cards (they're
   priced placeholders: judge their prompts and where they sit). Do mark down words the voice gets wrong (the heard
   line differs from the script: write numbers and names the way they're said).
3) Call critic_verdict once: the scores; a note for each beat with a problem; at most 6 fixes in order of impact,
   each concrete and doable with the producer's tools (rewrite a line or a text, change, trim or sync a shot, punch
   in on a spot, split a beat into quick cuts, drop or merge beats, re-record a moment, a stock clip, the pace),
   e.g. "b3 says 'it stopped at checkout' over the agent list: show approve-budget.mp4 from 1.2 s instead"; the
   verdict ("ship" only when every score is 4 or more, "rethink" when the idea itself doesn't work for this
   product); a one-line summary; and at most one lesson that would help any short in this format (not about this
   product). Then finish. Never ask the human anything and never edit the short yourself."""


def _emit(text: str, data: dict[str, Any]) -> None:
    get_ctx().emit(get_agent_id(), "step", text, data)


def _dir(slug: str) -> str:
    return f"video/{slug}/critique"


def fingerprint(script: dict[str, Any], hook: str) -> str:
    """What the cut is made of: a new critique only makes sense when this changed."""
    keep = {k: script.get(k) for k in ("hooks", "beats", "pace", "text_style", "captions", "music", "sound",
                                       "end_hold_s")}
    keep["take"] = ((script.get("takes") or {}).get(hook) or {}).get("text")
    return hashlib.sha256(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def overall(scores: dict[str, int]) -> float:
    """Saying and showing count double: it's the first thing a viewer notices when it's off."""
    return round((2 * scores["say_show"] + sum(scores[k] for k in SCORES if k != "say_show")) / (len(SCORES) + 1), 2)


# ------------------------------------------------------------------------------------------ what the cut is
def _beat_at(beats: list[dict[str, Any]], t: float) -> dict[str, Any] | None:
    return next((b for b in beats if b["start_s"] <= t < b["end_s"]), beats[-1] if beats else None)


def _words(words: list[dict[str, Any]], a: float, b: float) -> str:
    return " ".join(str(w.get("word") or w.get("text") or "").strip() for w in words if a <= float(w["start"]) < b)


def _texts_at(timeline: dict[str, Any], t: float) -> list[str]:
    return [o["text"] for o in timeline.get("overlays") or [] if o.get("position") != "tag" and
            o["start"] <= t < o["end"]]


def _planned_words(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for pg in timeline.get("captions") or [] for w in pg["words"]]


def labels(timeline: dict[str, Any], report: dict[str, Any], heard: list[dict[str, Any]],
           times: list[float]) -> list[str]:
    """Under each filmstrip frame: when, which beat, what's being said then and what's written on screen."""
    words = heard or _planned_words(timeline)
    out = []
    for t in times:
        b = _beat_at(report["beats"], t)
        said = _words(words, t - 0.6, t + 0.3)
        texts = " / ".join(_texts_at(timeline, t))
        out.append(f"{t:.1f}s {b['id'] if b else ''} · says: " + (f"\"{said}\"" if said else "—")
                   + (f" · text: {texts}" if texts else ""))
    return out


def _shot_line(item: dict[str, Any], fps: int) -> str:
    if item["kind"] == "placeholder":
        return f"AI-shot placeholder ({item.get('label', '')[:160]})"
    what = "still " if item["kind"] == "image" else ""
    s = f"{what}{item.get('src')}"
    if item["kind"] == "clip":
        s += f" from {item.get('in_s', 0):g}s" + (f" at {item['speed']:g}×" if abs(item.get("speed", 1) - 1) > 0.01
                                                    else "")
    if item.get("crop"):
        c = item["crop"]
        s += f", punched in {c['zoom']:g}× at ({c['x']:g}, {c['y']:g})"
    return f"{s} for {item['frames'] / fps:.1f}s"


def watch_log(script: dict[str, Any], timeline: dict[str, Any], report: dict[str, Any], watched: dict[str, Any],
              card: dict[str, Any] | None) -> str:
    """The cut as text, beat by beat: what's shown, what the script says, what the soundtrack actually says."""
    fps = int(timeline.get("fps") or sp.FPS)
    heard = watched.get("heard") or []
    lines = [f"\"{script['title']}\" ({timeline['hook']}): {report['duration_s']:.1f}s, {len(timeline['video'])} "
             f"pieces of video, about {report['avg_shot_s']}s each. Audio: "
             + ("voice" if timeline.get("voice") else "none (text + sound: the sound is added in the app)")
             + (f" ({script['sound']})" if script.get("sound") else "")
             + f". Captions: {script.get('captions') or ('phrase' if timeline.get('voice') else 'none')}."]
    if card:
        c = card["card"]
        m = c.get("measured") or {}
        lines.append(f"Format card \"{card['name']}\": audio {c.get('audio')}, text {c.get('text_style') or '?'}; "
                     + (f"the examples run {m.get('length_s')}s with a shot every {m.get('avg_shot_s')}s. "
                        if m else "")
                     + f"Why it works: {c.get('why', '')} Adapt: {c.get('adapt', '')}")
        for i, sh in enumerate((c.get("shots") or [])[:16], 1):
            lines.append(f"  card shot {i}: {sh.get('type')} {sh.get('seconds')}s: {sh.get('shows', '')}"
                         + (f" | text: {sh['text']}" if sh.get("text") else "")
                         + (f" | said: {sh['said']}" if sh.get("said") else ""))
        if c.get("lessons"):
            lines.append("Lessons from earlier critiques of this format: " + " | ".join(c["lessons"]))
    lines.append("Beat by beat:")
    parts = {p["id"]: p for p in [*script["hooks"], *script["beats"]]}
    at_f = 0
    items = timeline["video"]
    starts = []
    for it in items:
        starts.append(at_f / fps)
        at_f += it["frames"]
    for b in report["beats"]:
        p = parts.get(b["id"], {})
        shown = [_shot_line(it, fps) for it, st in zip(items, starts) if b["start_s"] - 1e-6 <= st < b["end_s"] - 1e-6]
        lines.append(f" {b['id']} {b['start_s']:.1f}–{b['end_s']:.1f}s: shows " + " + ".join(shown))
        if p.get("vo"):
            got = _words(heard, b["start_s"], b["end_s"]) if heard else ""
            lines.append(f"    script says: \"{p['vo']}\"" + (f"   heard: \"{got}\"" if heard else ""))
        texts = sorted({o["text"] for o in timeline.get("overlays") or [] if o.get("position") != "tag"
                        and b["start_s"] - 1e-6 <= o["start"] < b["end_s"] - 1e-6})
        if texts:
            lines.append(f"    on screen: " + " / ".join(f"\"{t}\"" for t in texts))
        if b.get("warnings"):
            lines.append("    plan notes: " + "; ".join(b["warnings"]))
    fz = ", ".join(f"{a:.1f}–{e:.1f}s" for a, e in watched.get("freezes") or []) or "none"
    bl = ", ".join(f"{a:.1f}–{e:.1f}s" for a, e in watched.get("blacks") or []) or "none"
    si = "the whole cut (no soundtrack yet)" if watched.get("silent") else \
        (", ".join(f"{a:.1f}–{e:.1f}s" for a, e in watched.get("silences") or []) or "none")
    lines.append(f"Measured: picture frozen {fz}; black {bl}; silence {si}.")
    return "\n".join(lines)


def findings(script: dict[str, Any], timeline: dict[str, Any], report: dict[str, Any],
             watched: dict[str, Any]) -> list[str]:
    """What Todd can tell without judging: the voice saying something else, a frozen picture, dead air, black frames,
    nothing to read or hear at the start, and the plan's own warnings."""
    out: list[str] = []
    heard = watched.get("heard") or []
    parts = {p["id"]: p for p in [*script["hooks"], *script["beats"]]}
    voiced = bool(timeline.get("voice"))
    for b in report["beats"]:
        p = parts.get(b["id"], {})
        if voiced and p.get("vo") and heard:
            got = _words(heard, b["start_s"] - 0.1, b["end_s"])
            norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower())  # noqa: E731
            ratio = difflib.SequenceMatcher(None, norm(p["vo"]), norm(got)).ratio()
            if ratio < HEARD_MATCH:
                out.append(f"{b['id']}: the voice says \"{got}\" where the script says \"{p['vo']}\": write it the way "
                           "it should be said (numbers and names spelled out as spoken)")
    for a, e in watched.get("freezes") or []:
        if e - a >= 1.5:
            b = _beat_at(report["beats"], (a + e) / 2)
            out.append(f"{b['id'] if b else '?'}: the picture doesn't move for {e - a:.1f}s ({a:.1f}–{e:.1f}s)")
    for a, e in watched.get("blacks") or []:
        out.append(f"black picture {a:.1f}–{e:.1f}s")
    if voiced and not watched.get("silent"):
        for a, e in watched.get("silences") or []:
            if e - a >= 1.0 and e < report["duration_s"] - 0.3 and a > 0.05:
                out.append(f"{e - a:.1f}s of silence at {a:.1f}–{e:.1f}s")
        lead = next((e for a, e in watched.get("silences") or [] if a <= 0.05), 0.0)
        if lead > 0.4:
            out.append(f"the voice starts {lead:.1f}s in: the hook should be heard at once")
    if not _texts_at(timeline, 0.05) and not (voiced and heard and float(heard[0]["start"]) < 0.6):
        out.append("nothing to read or hear in the first half second: the hook's text belongs on the first frame")
    lengths = [it["frames"] for it in timeline["video"]]
    if len(lengths) >= 4 and statistics.pstdev(lengths) < 0.08 * statistics.mean(lengths):
        out.append("every shot is the same length: a template tell; vary them")
    out += [w for w in report.get("warnings") or [] if w not in out]
    return out[:20]


# ------------------------------------------------------------------------------------------ the package
def _card(script: dict[str, Any]) -> dict[str, Any] | None:
    fid = (script.get("format") or {}).get("id")
    if not fid:
        return None
    from ..db import FormatCard, session

    with session() as s:
        row = s.get(FormatCard, fid)
    if row is None:
        return None
    return {"id": row.id, "name": row.name, "card": row.card or {}, "examples": row.examples, "run_id": row.run_id}


async def _raw(base: str, path: str) -> str | None:
    try:
        r = await sandbox.call("/files/view", {"base": base, "path": path, "raw": True}, timeout=30)
    except ToolError:
        return None
    return r.get("data")


async def _example_sheets(card: dict[str, Any] | None, limit: int = 2) -> list[str]:
    """The shot sheets of the card's real examples (from the run that studied them), base64."""
    if not card or not card.get("run_id"):
        return []
    base = f"{config.workspace_root}/{card['run_id']}"
    try:
        r = await sandbox.call("/files/tree", {"base": f"{base}/video/research"}, timeout=30)
    except ToolError:
        return []
    ids = [str(e.get("id")) for e in card.get("examples") or []]
    found = []
    for i in ids:
        hit = next((e["path"] for e in r.get("entries") or [] if e["path"].rsplit("/", 1)[-1] in (f"{i}.jpg",
                                                                                               f"{i}.png")), None)
        if hit:
            data = await _raw(base, f"video/research/{hit}")
            if data:
                found.append(data)
        if len(found) >= limit:
            break
    return found


async def package(slug: str, hook: str, version: int) -> dict[str, Any]:
    """Render the cut clean (no beat labels), watch it, and write what the critic gets to video/<slug>/critique/."""
    from .shorts import _plan, _read_json, _write_json  # noqa: F401  (the shorts module owns the script files)

    ctx = get_ctx()
    script, plans = await _plan(slug, [hook])
    p = plans[hook]
    video = f"{_dir(slug)}/{slug}-{hook}-v{version}.mp4"
    r = await media.render_timeline(ctx.run_id, video, p.timeline)
    watched = await media.call("/watch", {"run_id": ctx.run_id, "src": video}, timeout=600)
    dur = float(r["duration_s"])
    fps = STRIP_FPS if dur * STRIP_FPS <= STRIP_MAX else STRIP_MAX / dur
    times = [round((k + 0.5) / fps, 2) for k in range(max(1, int(dur * fps)))]
    strip = await media.call("/filmstrip", {
        "run_id": ctx.run_id, "src": video, "out": f"{_dir(slug)}/{slug}-{hook}-v{version}-strip.jpg",
        "times": times, "labels": labels(p.timeline, p.report, watched.get("heard") or [], times), "inline": False},
        timeout=600)
    card = _card(script)
    pkg = {"slug": slug, "hook": hook, "version": version, "video": video, "duration_s": dur,
           "every_s": round(1 / fps, 2), "sheets": strip["sheets"], "card_id": card["id"] if card else None,
           "log": watch_log(script, p.timeline, p.report, watched, card),
           "findings": findings(script, p.timeline, p.report, watched),
           "shots": len(p.timeline["video"]), "avg_shot_s": p.report["avg_shot_s"],
           "fingerprint": fingerprint(script, hook)}
    await _write_json(f"{_dir(slug)}/{hook}-v{version}.json", pkg)
    return pkg


async def _show_package(pkg: dict[str, Any], examples: bool = True) -> int:
    """Push the filmstrip (and the card's example sheets) to the calling agent; how many images."""
    ctx, agent = get_ctx(), get_agent_id()
    from .sandbox_tools import _root

    n = 0
    for path in pkg["sheets"][:9]:
        data = await _raw(_root(), path)
        if data:
            ctx.push_image(agent, data, "image/jpeg")
            n += 1
    if examples:
        for data in await _example_sheets(_card_by_id(pkg.get("card_id")), limit=2):
            ctx.push_image(agent, data, "image/jpeg")
            n += 1
    return n


def _card_by_id(fid: str | None) -> dict[str, Any] | None:
    return _card({"format": {"id": fid}}) if fid else None


# ------------------------------------------------------------------------------------------ the critic's tools
@todd_tool(toolset="critic")
async def critic_watch(slug: str, hook: str, version: int) -> dict:
    """Watch the cut you're critiquing: the filmstrip sheets (a frame every half second, labelled with the time, the
    beat, what's said then and what's on screen) and, after them, sheets of the format card's real examples, come
    with this result as images; the log, Todd's measurements and checks are below.

    Args:
        slug: the short
        hook: the cut's hook variant, e.g. "h1"
        version: the critique version from your task
    """
    from .shorts import _read_json

    pkg = await _read_json(f"{_dir(slug)}/{hook}-v{int(version)}.json")
    if pkg is None:
        raise ToolError(f"no critique package {slug} {hook} v{version}: use the slug, hook and version in your task")
    n = await _show_package(pkg)
    return {"video": pkg["video"], "duration_s": pkg["duration_s"],
            "images": f"{n} sheets: the filmstrip first (a frame every {pkg['every_s']}s), then the format's real "
                      "examples (a frame from each of their shots) if any",
            "log": pkg["log"], "todd_checks": pkg["findings"],
            "next": "judge it beat by beat, then critic_verdict once, then finish"}


def _verdict_path(slug: str, hook: str, version: int) -> str:
    return f"{_dir(slug)}/{hook}-v{int(version)}-verdict.json"


@todd_tool(toolset="critic")
async def critic_verdict(slug: str, hook: str, version: int, scores: dict, verdict: str, summary: str,
                         fixes: list[dict] | None = None, beats: list[dict] | None = None, lesson: str = "") -> dict:
    """Give your verdict on the cut, once.

    Args:
        slug: the short
        hook: the hook variant
        version: the critique version
        scores: 1–5 each: {"hook", "say_show", "organic", "timing", "format_fit"}
        verdict: "ship" (every score 4+), "fix" or "rethink" (the idea doesn't work for this product)
        summary: one line a human can read at a glance
        fixes: up to 6, most important first: [{"part": "b3" (or "h1", "all"), "problem": what's wrong,
            "fix": the concrete change, doable with the producer's tools}]
        beats: notes per beat with a problem: [{"id": "b3", "say_show": "match" | "weak" | "mismatch", "note": "…"}]
        lesson: at most one lesson for any short in this format (not about this product), or ""
    """
    try:
        sc = {k: int(scores[k]) for k in SCORES}
    except (KeyError, TypeError, ValueError):
        raise ToolError(f"scores is {{{', '.join(SCORES)}}}, each 1–5") from None
    if not all(1 <= v <= 5 for v in sc.values()):
        raise ToolError("each score is 1–5")
    if verdict not in VERDICTS:
        raise ToolError(f"verdict is one of {', '.join(VERDICTS)}")
    if verdict == "ship" and min(sc.values()) < 4:
        raise ToolError("\"ship\" means every score is 4 or more: say \"fix\" and give the fixes")
    fx = []
    for f in (fixes or [])[:MAX_FIXES]:
        if not isinstance(f, dict) or not str(f.get("fix") or "").strip():
            raise ToolError("each fix is {\"part\", \"problem\", \"fix\"}")
        fx.append({"part": str(f.get("part") or "all")[:8], "problem": " ".join(str(f.get("problem") or "").split())[:300],
                   "fix": " ".join(str(f["fix"]).split())[:400]})
    if verdict == "fix" and not fx:
        raise ToolError("a \"fix\" verdict needs at least one fix")
    out = {"slug": slug, "hook": hook, "version": int(version), "scores": sc, "overall": overall(sc),
           "verdict": verdict, "summary": " ".join(str(summary or "").split())[:300], "fixes": fx,
           "beats": [{"id": str(b.get("id"))[:8], "say_show": str(b.get("say_show") or "")[:10],
                      "note": " ".join(str(b.get("note") or "").split())[:300]} for b in (beats or [])[:16]
                     if isinstance(b, dict)],
           "lesson": " ".join(str(lesson or "").split())[:200], "by": get_agent_id()}
    from .shorts import _write_json

    await _write_json(_verdict_path(slug, hook, version), out)
    return {"saved": True, "overall": out["overall"], "next": "finish now"}


CRITIC_TOOLS = [critic_watch, critic_verdict]


# ------------------------------------------------------------------------------------------ the producer's tool
async def _run_critic(slug: str, hook: str, version: int) -> tuple[dict[str, Any] | None, str]:
    """A fresh critic agent judges the package; (its verdict, or None and why not)."""
    from ..agents import dynamic
    from .shorts import _read_json

    ctx = get_ctx()
    task = (f"Critique the short \"{slug}\", hook {hook}, version {version}: critic_watch(slug=\"{slug}\", "
            f"hook=\"{hook}\", version={version}), then critic_verdict with the same three, then finish.")
    try:
        handle = await dynamic.spawn(ctx, name=f"Critic: {slug[:28]} {hook} v{version}", instructions=CRITIC_GUIDE,
                                     task=task, toolsets=["critic"], parent=get_agent_id())
    except ToolError as e:
        return None, f"the critic couldn't start: {e}"
    try:
        await asyncio.wait_for(asyncio.shield(handle.task), CRITIC_TIMEOUT_S)
    except asyncio.TimeoutError:
        if handle.task and not handle.task.done():
            ctx.emit(handle.id, "status", "Cancelled: took too long")
            handle.task.cancel()
            await dynamic.wait([handle], timeout=10)
        return None, f"the critic took longer than {CRITIC_TIMEOUT_S // 60} minutes"
    verdict = await _read_json(_verdict_path(slug, hook, version))
    if verdict is None:
        return None, "the critic finished without a verdict"
    return verdict, ""


def _save_lesson(card_id: str | None, lesson: str) -> None:
    if not card_id or not lesson:
        return
    from ..db import FormatCard, session

    with session() as s:
        row = s.get(FormatCard, card_id)
        if row is None:
            return
        card = dict(row.card or {})
        lessons = [x for x in card.get("lessons") or [] if x.lower() != lesson.lower()]
        card["lessons"] = [*lessons, lesson][-MAX_LESSONS:]
        row.card = card
        s.add(row)
        s.commit()


@todd_tool(toolset="shorts")
async def short_critique(slug: str, hook: str = "h1") -> dict:
    """Have a critic watch the cut before the human does. Todd renders it, measures it (frozen picture, silence, what
    the soundtrack really says) and makes a filmstrip every half second; a separate critic agent watches that next to
    the format card's real examples and judges what's said and written against what's shown beat by beat, the hook,
    how organic it looks, the timing and the fit to the format. You get scores, the verdict and up to 6 concrete
    fixes: apply them (short_edit, short_record, short_stock, short_pace…) and critique again. It's free. At most 3
    critiques per cut, only after a change, none after it passes; it stops as soon as the score stops improving.

    Args:
        slug: the short, from short_new
        hook: the hook variant to critique
    """
    from .shorts import _load, _lock, _write_json

    script = await _load(slug)
    mine = [c for c in script.get("critiques") or [] if c.get("hook") == hook]
    if len(mine) >= MAX_CRITIQUES:
        raise ToolError(f"{MAX_CRITIQUES} critiques of {slug} {hook} are done: put it in front of the human "
                        "(short_review) with what's still open")
    if mine and mine[-1].get("verdict") == "ship":
        raise ToolError(f"the critic passed v{mine[-1]['version']}: short_review")
    fp = fingerprint(script, hook)
    if mine and mine[-1].get("fingerprint") == fp:
        raise ToolError(f"nothing changed since critique v{mine[-1]['version']}: apply its fixes first (or take it to "
                        "the human with short_review)")
    version = len(mine) + 1
    _emit(f"Critic watching {slug} {hook} (v{version})", {"slug": slug, "hook": hook, "version": version})
    pkg = await package(slug, hook, version)
    verdict, why_not = await _run_critic(slug, hook, version)
    rec: dict[str, Any] = {"version": version, "hook": hook, "video": pkg["video"], "fingerprint": pkg["fingerprint"],
                           "findings": len(pkg["findings"]),
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if verdict:
        rec |= {k: verdict[k] for k in ("scores", "overall", "verdict", "summary", "fixes", "lesson")}
        _save_lesson(pkg.get("card_id"), verdict.get("lesson") or "")
    else:
        rec |= {"verdict": None, "summary": why_not}
    async with _lock(slug):
        s2 = await _load(slug)
        s2.setdefault("critiques", []).append(rec)
        await _write_json(f"video/{slug}/script.json", s2)
    prev = next((c for c in reversed(mine) if c.get("overall") is not None), None)
    out: dict[str, Any] = {"version": version, "video": pkg["video"], "todd_checks": pkg["findings"]}
    if verdict is None:  # judge it yourself from the same material, once
        n = await _show_package(pkg)
        out |= {"critic": f"no verdict ({why_not}): the filmstrip ({n} sheets) is shown to you instead",
                "log": pkg["log"],
                "next": "judge it yourself as a critic would (what's said vs shown per beat, hook, organic, timing), "
                        "fix what's wrong, then short_review"}
        _emit(f"Critic v{version} of {slug}: no verdict", {"slug": slug, "why": why_not})
        return out
    out |= {"verdict": verdict["verdict"], "overall": verdict["overall"], "scores": verdict["scores"],
            "summary": verdict["summary"], "fixes": verdict["fixes"], "beats": verdict["beats"]}
    _emit(f"Critic v{version} of {slug}: {verdict['verdict']} ({verdict['overall']}/5) {verdict['summary']}",
          {"slug": slug, "version": version, "scores": verdict["scores"]})
    if verdict["verdict"] == "ship":
        out["next"] = "the critic passes it: short_review"
    elif prev and verdict["overall"] <= prev["overall"]:
        out["next"] = (f"no better than v{prev['version']} ({prev['overall']}/5): stop iterating; apply only fixes "
                       "you're sure of, then short_review and tell the human what's still open")
    elif version >= MAX_CRITIQUES:
        out["next"] = "last critique: apply the fixes you can, then short_review and tell the human what's still open"
    elif verdict["verdict"] == "rethink":
        out["next"] = ("the critic says the idea doesn't work for this product: rebuild it on another card "
                       "(short_new with a different format_id) or tell the human why at short_review")
    else:
        out["next"] = "apply the fixes (voice again if a line changed), short_plan, then short_critique again"
    return out
