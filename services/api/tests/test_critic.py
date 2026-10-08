"""The critic: what Todd measures in a rendered cut, how the critic sees it, and the guards that stop the
fix-and-critique loop. A fake media service and a fake critic agent: no FFmpeg, no model."""

from __future__ import annotations

import asyncio
import json

import pytest

from todd import registry
from todd.sdk import ToolError
from todd.tools import critic, shorts
from todd.tools import media as media_mod

from .test_shorts import HOOKS, BEATS, script_of, studio  # noqa: F401  (the fixture)

TIMELINE = {
    "hook": "h1", "fps": 30, "frames": 180, "duration_s": 6.0,
    "video": [{"kind": "clip", "src": "rec/a.mp4", "frames": 60, "in_s": 0, "speed": 1},
              {"kind": "clip", "src": "rec/b.mp4", "frames": 60, "in_s": 1.5, "speed": 1,
               "crop": {"x": 0.5, "y": 0.3, "zoom": 1.4}},
              {"kind": "placeholder", "frames": 60, "label": "Seedance 2.5 · 3 s · $0.22 · a goalie"}],
    "voice": {"src": "a.wav", "segments": []},
    "captions": [{"start": 0.1, "end": 1.0, "words": [{"text": "Ten", "start": 0.1, "end": 0.4},
                                                      {"text": "forty-four", "start": 0.4, "end": 1.0}]}],
    "overlays": [{"text": "it asked first", "start": 0.0, "end": 2.0, "position": "top", "style": "box"},
                 {"text": "b1 · 2.0s", "start": 2.0, "end": 4.0, "position": "tag"}],
}
REPORT = {"duration_s": 6.0, "avg_shot_s": 2.0, "warnings": ["b2: only 0.5s on screen"],
          "beats": [{"id": "h1", "start_s": 0.0, "end_s": 2.0, "warnings": []},
                    {"id": "b1", "start_s": 2.0, "end_s": 4.0, "warnings": []},
                    {"id": "b2", "start_s": 4.0, "end_s": 6.0, "warnings": ["only 0.5s on screen"]}]}
SCRIPT = {"title": "t", "sound": "", "captions": None,
          "hooks": [{"id": "h1", "vo": "Ten forty-four at Porkbun.", "text": "it asked first"}],
          "beats": [{"id": "b1", "vo": "Then it stopped."}, {"id": "b2", "vo": "", "seconds": 2}]}


def test_findings_catch_a_misheard_line_a_frozen_picture_and_a_silent_start():
    heard = [{"word": "10", "start": 0.7, "end": 0.9}, {"word": ".4", "start": 0.9, "end": 1.0},
             {"word": "port-bun", "start": 1.0, "end": 1.5},
             {"word": "Then", "start": 2.1, "end": 2.3}, {"word": "it", "start": 2.3, "end": 2.4},
             {"word": "stopped.", "start": 2.4, "end": 2.9}]
    watched = {"heard": heard, "freezes": [[3.0, 5.5]], "blacks": [], "silences": [[0.0, 0.7]], "silent": False}
    f = critic.findings(SCRIPT, TIMELINE, REPORT, watched)
    assert any(x.startswith("h1: the voice says \"10 .4 port-bun\"") for x in f)
    assert not any(x.startswith("b1: the voice says") for x in f)  # heard as written
    assert any("doesn't move for 2.5s" in x for x in f)
    assert any("the voice starts 0.7s in" in x for x in f)
    assert "b2: only 0.5s on screen" in f  # the plan's own warnings come along
    # a silent text + sound cut isn't faulted for silence, and text on the first frame is a hook
    quiet = critic.findings(SCRIPT, {**TIMELINE, "voice": None}, REPORT,
                            {"heard": [], "freezes": [], "silences": [[0, 6]], "silent": True})
    assert not any("silence" in x or "first half second" in x for x in quiet)
    bare = critic.findings(SCRIPT, {**TIMELINE, "voice": None, "overlays": []}, REPORT,
                           {"heard": [], "freezes": [], "silences": [], "silent": True})
    assert any("first half second" in x for x in bare)


def test_the_filmstrip_says_what_is_said_and_written_under_each_frame():
    heard = [{"word": "Ten", "start": 0.1, "end": 0.4}, {"word": "forty-four", "start": 0.4, "end": 1.0}]
    labs = critic.labels(TIMELINE, REPORT, heard, [0.25, 2.5, 4.75])
    assert labs[0] == "0.2s h1 · says: \"Ten forty-four\" · text: it asked first"
    assert labs[1] == "2.5s b1 · says: —"  # the scaffold's beat tag isn't on-screen text
    log = critic.watch_log(SCRIPT, TIMELINE, REPORT, {"heard": heard, "freezes": [[3, 5]], "silences": []}, None)
    assert " h1 0.0–2.0s: shows rec/a.mp4 from 0s for 2.0s" in log
    assert "punched in 1.4× at (0.5, 0.3)" in log and "AI-shot placeholder" in log
    assert "script says: \"Ten forty-four at Porkbun.\"   heard: \"Ten forty-four\"" in log
    assert "picture frozen 3.0–5.0s" in log


def test_scores_weigh_saying_against_showing_double():
    assert critic.overall({"hook": 5, "say_show": 2, "organic": 5, "timing": 5, "format_fit": 5}) == 4.0
    s = {"hooks": [{"id": "h1", "vo": "a"}], "beats": [], "takes": {"h1": {"text": "a"}}}
    a = critic.fingerprint(s, "h1")
    assert a == critic.fingerprint(json.loads(json.dumps(s)), "h1")
    s["beats"].append({"id": "b1", "vo": "b"})
    assert critic.fingerprint(s, "h1") != a


class FakeCritic:
    """Stands in for the critic agent: calls critic_watch and critic_verdict like the real one would."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.tasks = []

    async def spawn(self, ctx, *, name, instructions, task, toolsets, parent="planner", **_):
        assert toolsets == ["critic"] and "critic_verdict" in instructions
        self.tasks.append(task)
        slug = task.split('"')[1]
        version = int(task.split("version ")[1].split(":")[0])
        v = self.verdicts.pop(0)

        async def run():
            await critic.critic_watch.ainvoke({"slug": slug, "hook": "h1", "version": version})
            if v is not None:
                await critic.critic_verdict.ainvoke({"slug": slug, "hook": "h1", "version": version, **v})

        class H:
            id = "critic-1"
            task = asyncio.ensure_future(run())
        return H()


def _v(score, verdict="fix", lesson=""):
    return {"scores": {k: score for k in critic.SCORES}, "verdict": verdict, "summary": f"scored {score}",
            "fixes": [] if verdict == "ship" else [{"part": "b2", "problem": "says X shows Y", "fix": "show Y"}],
            "lesson": lesson}


def test_critique_loop_runs_with_guards(loop, studio, monkeypatch):  # noqa: F811
    from todd.agents import dynamic
    from todd.db import FormatCard, session

    with session() as db:
        card = FormatCard(name="pov one-liner", tags=["x"], card={"why": "y", "audio": "voiceover"},
                          examples=[{"id": "1", "plays": 5}])
        db.add(card)
        db.commit()
        db.refresh(card)
    fake = FakeCritic([_v(2, lesson="put the punchline on screen by 3 s"), _v(3), _v(3), _v(5, "ship")])
    monkeypatch.setattr(dynamic, "spawn", fake.spawn)
    media = studio["media"]
    orig = media.call

    async def call(path, payload, timeout=120):
        if path == "/watch":
            media.calls.append((path, payload))
            return {"duration_s": 5.0, "freezes": [], "blacks": [], "silences": [], "silent": False,
                    "cuts": [], "heard": [{"word": "Every", "start": 0.3, "end": 0.6}]}
        if path == "/filmstrip":
            media.calls.append((path, payload))
            out = studio["dir"] / payload["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\xff\xd8\xff strip")
            return {"sheets": [payload["out"]]}
        return await orig(path, payload, timeout)

    monkeypatch.setattr(media_mod, "call", call)

    async def go():
        r = await shorts.short_new.ainvoke({"title": "loop", "hooks": HOOKS[:1], "beats": BEATS,
                                            "format_id": card.id})
        slug = r["slug"]
        await shorts.short_voiceover.ainvoke({"slug": slug})
        with pytest.raises(ToolError, match="short_critique"):
            await shorts.short_review.ainvoke({"slug": slug})  # the critic watches before the human does
        c1 = await shorts.short_critique.ainvoke({"slug": slug})
        assert c1["verdict"] == "fix" and c1["overall"] == 2 and c1["fixes"][0]["fix"] == "show Y"
        assert "short_critique again" in c1["next"]
        strip = media.last("/filmstrip")
        assert strip["times"][:2] == [0.25, 0.75] and len(strip["times"]) == len(strip["labels"])
        assert "says:" in strip["labels"][0]
        assert "critique/" in media.last("/render/timeline")["out"]
        assert not [o for o in media.last("/render/timeline")["overlays"] if o["position"] == "tag"]  # clean cut
        with session() as db:
            assert db.get(FormatCard, card.id).card["lessons"] == ["put the punchline on screen by 3 s"]
        with pytest.raises(ToolError, match="nothing changed since critique v1"):
            await shorts.short_critique.ainvoke({"slug": slug})
        await shorts.short_edit.ainvoke({"slug": slug, "part": "b1", "text": "the pro-gear guy"})
        c2 = await shorts.short_critique.ainvoke({"slug": slug})
        assert c2["overall"] == 3 and "short_critique again" in c2["next"]
        await shorts.short_edit.ainvoke({"slug": slug, "part": "b1", "text": "pro-gear guy"})
        c3 = await shorts.short_critique.ainvoke({"slug": slug})
        assert "no better than v2" in c3["next"]  # it stopped improving: stop iterating
        await shorts.short_edit.ainvoke({"slug": slug, "part": "b1", "text": "the pro gear guy"})
        with pytest.raises(ToolError, match="3 critiques"):
            await shorts.short_critique.ainvoke({"slug": slug})
        s = script_of(studio, slug)
        assert [c["version"] for c in s["critiques"]] == [1, 2, 3] and s["critiques"][0]["overall"] == 2
        assert len(fake.tasks) == 3
        # rebuilding as a new short doesn't reset the producer's allowance
        fake.verdicts = [_v(2), _v(2), _v(2)]
        again = (await shorts.short_new.ainvoke({"title": "loop again", "hooks": HOOKS[:1], "beats": BEATS}))["slug"]
        await shorts.short_voiceover.ainvoke({"slug": again})
        await shorts.short_critique.ainvoke({"slug": again})
        await shorts.short_edit.ainvoke({"slug": again, "part": "b1", "text": "z"})
        await shorts.short_critique.ainvoke({"slug": again})
        await shorts.short_edit.ainvoke({"slug": again, "part": "b1", "text": "zz"})
        with pytest.raises(ToolError, match="5 critiques across your shorts"):
            await shorts.short_critique.ainvoke({"slug": again})
    loop.run_until_complete(go())


def test_a_pass_ends_it_and_a_silent_critic_falls_back_to_the_producer(loop, studio, monkeypatch):  # noqa: F811
    from todd.agents import dynamic
    from todd.sdk import get_agent_id, get_ctx

    fake = FakeCritic([None, _v(5, "ship")])
    monkeypatch.setattr(dynamic, "spawn", fake.spawn)
    media = studio["media"]
    orig = media.call

    async def call(path, payload, timeout=120):
        if path == "/watch":
            return {"duration_s": 5.0, "freezes": [], "blacks": [], "silences": [], "silent": False, "heard": []}
        if path == "/filmstrip":
            out = studio["dir"] / payload["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\xff\xd8\xff strip")
            return {"sheets": [payload["out"]]}
        return await orig(path, payload, timeout)

    monkeypatch.setattr(media_mod, "call", call)

    async def go():
        slug = (await shorts.short_new.ainvoke({"title": "pass", "hooks": HOOKS[:1], "beats": BEATS}))["slug"]
        await shorts.short_voiceover.ainvoke({"slug": slug})
        c1 = await shorts.short_critique.ainvoke({"slug": slug})
        assert "no verdict" in c1["critic"] and "judge it yourself" in c1["next"] and "log" in c1
        assert get_ctx().pop_images(get_agent_id())  # the producer sees the filmstrip instead
        await shorts.short_edit.ainvoke({"slug": slug, "part": "b1", "text": "x"})
        c2 = await shorts.short_critique.ainvoke({"slug": slug})
        assert c2["verdict"] == "ship" and "short_review" in c2["next"]
        await shorts.short_edit.ainvoke({"slug": slug, "part": "b1", "text": "y"})
        with pytest.raises(ToolError, match="passed v2"):
            await shorts.short_critique.ainvoke({"slug": slug})
    loop.run_until_complete(go())


def test_verdicts_are_checked_and_the_critic_toolset_stays_out_of_the_planners_list(loop, studio):  # noqa: F811
    async def go():
        base = {"slug": "s", "hook": "h1", "version": 1, "summary": "x"}
        with pytest.raises(ToolError, match="every score is 4"):
            await critic.critic_verdict.ainvoke({**base, **_v(3, "ship")})
        with pytest.raises(ToolError, match="scores is"):
            await critic.critic_verdict.ainvoke({**base, "scores": {"hook": 3}, "verdict": "fix"})
        with pytest.raises(ToolError, match="needs at least one fix"):
            await critic.critic_verdict.ainvoke({**base, "scores": {k: 3 for k in critic.SCORES},
                                                 "verdict": "fix", "fixes": []})
    loop.run_until_complete(go())
    ts = registry.all_toolsets()
    assert ts["critic"].hidden and "`critic`" not in registry.toolsets_prompt(ts)
    assert [t.name for t in ts["critic"].tools] == ["critic_watch", "critic_verdict"]
