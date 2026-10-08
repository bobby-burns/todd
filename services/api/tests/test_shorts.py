"""The shorts toolset end to end: script → voiceover (one charge) → plan → animatic. The real sandbox server on a temp
folder, a fake media service and a fake ElevenLabs that returns realistic character timestamps: no network, no
FFmpeg."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from todd import cdp, registry, screencast, vault
from todd.config import config
from todd.db import LedgerEntry, select, session
from todd.integrations import find_integrations
from todd.runtime import RunContext
from todd.sdk import ToolError, _current_ctx, set_ctx
from todd.tools import elevenlabs, media, sandbox, shorts

from .helpers import events_of, new_run

SERVER = Path(__file__).resolve().parents[2] / "sandbox" / "server.py"
PNG = b"\x89PNG\r\n\x1a\n a contact sheet"


def fake_take(text: str) -> dict:
    """Character timestamps like ElevenLabs': 0.3 s of silence, 0.28 s a word, a longer pause after a sentence."""
    chars, starts, ends = [], [], []
    t = 0.3
    for i, word in enumerate(text.split(" ")):
        if i:
            gap = 0.3 if chars[-1] in ".!?" else 0.04
            chars.append(" ")
            starts.append(t)
            ends.append(t + gap)
            t += gap
        step = 0.28 / len(word)
        for ch in word:
            chars.append(ch)
            starts.append(round(t, 4))
            ends.append(round(t + step, 4))
            t += step
    return {"characters": chars, "character_start_times_seconds": starts, "character_end_times_seconds": ends}


class FakeMedia:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.durations: dict[str, float] = {}  # video path -> seconds
        self.calls: list[tuple[str, dict]] = []

    async def call(self, path: str, payload: dict, timeout: float = 120) -> dict:
        self.calls.append((path, payload))
        run = self.root / payload["run_id"]
        if path == "/files/put":
            p = run / payload["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(payload["data_b64"]))
            return {"path": payload["path"], "size_bytes": p.stat().st_size, "sha256": "x"}
        if path == "/probe":
            items = []
            for q in payload["paths"]:
                if not (run / q).is_file():
                    items.append({"ok": False, "path": q, "error": "no such file"})
                elif q.endswith(".png"):
                    items.append({"ok": True, "path": q, "kind": "image", "width": 1170, "height": 2532})
                else:
                    items.append({"ok": True, "path": q, "kind": "video", "duration_s": self.durations.get(q, 5.0)})
            return {"items": items}
        if path == "/tts/local":
            out = run / payload["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"RIFF fake wav")
            al = fake_take(payload["text"])
            return {"path": payload["out"], "duration_s": al["character_end_times_seconds"][-1] + 0.3,
                    "alignment": al, "voice": "fake"}
        if path == "/screencast/put":
            return {"stored": len(payload["frames"])}
        if path == "/screencast/assemble":
            out = run / payload["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00\x00\x00\x20ftypisom recording")
            return {"path": payload["out"], "duration_s": payload["end_s"], "frames": len(payload["times"]),
                    "size_bytes": 10, "width": 1080, "height": 1920}
        if path == "/render/timeline":
            out = run / payload["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00\x00\x00\x20ftypisom placeholder")
            frames = sum(v["frames"] for v in payload["video"])
            return {"path": payload["out"], "duration_s": round(frames / payload["fps"], 3), "frames": frames,
                    "size_bytes": out.stat().st_size}
        if path == "/thumbs":
            return {"sheet": payload["out"], "png_b64": base64.b64encode(b"thumbs").decode(), "missing": []}
        if path == "/fetch/video":
            out = run / payload["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00\x00\x00\x20ftypisom stock")
            self.durations[payload["out"]] = 12.0
            return {"path": payload["out"], "kind": "video", "duration_s": 12.0, "width": 1080, "height": 1920,
                    "fps": 30, "size_bytes": 20}
        if path == "/frames":  # a contact sheet, handed back inline for the agent to look at
            (run / payload["out"]).write_bytes(PNG)
            return {"sheet": payload["out"], "times": payload["times"], "duration_s": 5.0,
                    "png_b64": base64.b64encode(PNG).decode()}
        raise AssertionError(f"unexpected media call {path}")

    def last(self, path: str) -> dict:
        return [b for p, b in self.calls if p == path][-1]


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    spec = importlib.util.spec_from_file_location("todd_sandbox_server_shorts", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    client = TestClient(mod.app)
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))

    async def box_call(path, payload, timeout=120, **_):
        r = client.post(path, json=payload, headers={"X-Sandbox-Token": "t"})
        if r.status_code >= 400:
            raise ToolError(f"sandbox {path} -> {r.status_code}: {r.text[:1000]}")
        return r.json()

    monkeypatch.setattr(sandbox, "call", box_call)
    fake = FakeMedia(tmp_path)
    monkeypatch.setattr(media, "call", fake.call)
    spoken: list[dict] = []

    async def speak(key, voice_id, text, model_id="eleven_multilingual_v2", settings=None, seed=None):
        assert key == "el-test-key"
        spoken.append({"voice": voice_id, "text": text, "model": model_id})
        return {"audio": b"ID3 fake mp3", "alignment": fake_take(text), "request_id": f"req-{len(spoken)}"}

    async def voices(key, search=None):
        return [{"voice_id": "v-clone", "name": "Mine", "category": "cloned", "labels": {}},
                {"voice_id": "v-brian", "name": "Brian", "category": "premade", "labels": {"use_case": "narration"}}]

    monkeypatch.setattr(elevenlabs, "speak", speak)
    monkeypatch.setattr(elevenlabs, "voices", voices)
    rid = new_run("shorts")
    d = tmp_path / rid
    (d / "shots").mkdir(parents=True)
    (d / "shots" / "bench.png").write_bytes(b"png")
    (d / "shots" / "rec.mp4").write_bytes(b"mp4")
    tok = set_ctx(RunContext(rid))
    yield {"run": rid, "dir": d, "media": fake, "spoken": spoken}
    _current_ctx.reset(tok)
    vault.delete_secret("ELEVENLABS_API_KEY")


HOOKS = [{"vo": "Every drop-in has the same five guys.", "text": "every drop-in has these 5 guys",
          "shot": {"source": "image", "path": "shots/bench.png"}},
         {"vo": "Rating every guy at drop-in.", "shot": {"source": "image", "path": "shots/bench.png"}}]
BEATS = [{"vo": "Full pro gear. Hasn't scored since high school.", "text": "1. the pro-gear guy",
          "shot": {"source": "ai", "prompt": "a rec hockey player in full pro gear, wobbly crossover"}},
         {"vo": "And the one who already checked.", "text": "4. the one who checked",
          "shot": {"source": "screen", "path": "shots/rec.mp4", "sync": {"word": "checked", "at_s": 2.0}}}]


async def make() -> str:
    return (await shorts.short_new.ainvoke({"title": "Drop-in roster", "hooks": HOOKS, "beats": BEATS}))["slug"]


def script_of(studio, slug) -> dict:
    return json.loads((studio["dir"] / "video" / slug / "script.json").read_text())


def test_short_new_validates_and_writes_the_script(loop, studio):
    async def go():
        bad = [({"hooks": []}, "1–3 hooks"),
               ({"beats": [{"vo": "x", "shot": {"source": "drone"}}]}, "shot needs a source"),
               ({"beats": [{"vo": "x", "shot": {"source": "ai"}}]}, "prompt is empty"),
               ({"beats": [{"vo": "x", "shot": {"source": "screen", "path": "../other/rec.mp4"}}]}, "outside"),
               ({"beats": [{"vo": "x", "shot": {"source": "screen", "path": "shots/bench.png"}}]}, "videos"),
               ({"beats": [{"vo": "[excited] hi", "shot": {"source": "image", "path": "shots/bench.png"}}]},
                "plain words"),
               ({"beats": [{"vo": "x", "hold_s": 9, "shot": {"source": "image", "path": "shots/bench.png"}}]},
                "hold_s"),
               ({"beats": [{"vo": " ".join(["word"] * 30), "shot": {"source": "image", "path": "shots/bench.png"}}]
                 * 10}, "allows 60s"),
               ({"platform": "myspace"}, "platform")]
        for kw, msg in bad:
            with pytest.raises(ToolError, match=msg):
                await shorts.short_new.ainvoke({"title": "t", "hooks": HOOKS, "beats": BEATS, **kw})
        r = await shorts.short_new.ainvoke({"title": "Drop-in roster", "hooks": HOOKS, "beats": BEATS})
        assert r["slug"] == "drop-in-roster" and 3 < r["estimate_s"] < 12
        s = script_of(studio, r["slug"])
        assert [p["id"] for p in s["hooks"]] == ["h1", "h2"] and [p["id"] for p in s["beats"]] == ["b1", "b2"]
        assert s["beats"][0]["shot"] == {"source": "ai", "prompt": BEATS[0]["shot"]["prompt"], "model": "seedance-2.5"}
        assert s["beats"][1]["shot"]["sync"] == {"word": "checked", "at_s": 2.0}
        assert (await make()) == "drop-in-roster-2"
    loop.run_until_complete(go())


def test_voiceover_asks_for_its_key_then_voices_every_hook_for_one_charge(loop, studio, monkeypatch):
    from todd.sdk import get_ctx
    asked: list[tuple[str, dict]] = []

    async def go():
        async def later(question, agent=None, data=None):
            asked.append((question, data))
            return "later"

        async def paste(question, agent=None, data=None):  # the card saves the key straight to the vault
            asked.append((question, data))
            vault.set_secret(data["secret_name"], "el-test-key")
            return ""

        slug = await make()
        monkeypatch.setattr(get_ctx(), "ask_human", later)
        with pytest.raises(ToolError, match="didn't add ELEVENLABS_API_KEY"):
            await shorts.short_voiceover.ainvoke({"slug": slug, "provider": "elevenlabs"})
        assert "elevenlabs.io/app/settings/api-keys" in asked[0][0]
        assert asked[0][1] == {"secret_name": "ELEVENLABS_API_KEY"}
        monkeypatch.setattr(get_ctx(), "ask_human", paste)
        r = await shorts.short_voiceover.ainvoke({"slug": slug, "provider": "elevenlabs"})
        assert len(asked) == 2
        assert set(r["takes"]) == {"h1", "h2"} and r["voice_id"] == "v-brian"  # a premade voice by default
        assert [x["text"] for x in studio["spoken"]] == [
            "Every drop-in has the same five guys. Full pro gear. Hasn't scored since high school. "
            "And the one who already checked.",
            "Rating every guy at drop-in. Full pro gear. Hasn't scored since high school. "
            "And the one who already checked."]
        with session() as s:
            entries = s.exec(select(LedgerEntry).where(LedgerEntry.run_id == studio["run"])).all()
        assert len(entries) == 1 and entries[0].merchant == "ElevenLabs" and entries[0].status == "completed"
        chars = sum(len(x["text"]) for x in studio["spoken"])
        assert entries[0].amount_usd == max(0.01, -(-chars * 0.08 // 10) / 100)
        d = studio["dir"] / "video" / slug
        assert (d / "audio" / "vo-h1.mp3").read_bytes() == b"ID3 fake mp3"
        take = json.loads((d / "audio" / "vo-h1.json").read_text())
        assert take["alignment"]["characters"][0] == "E" and take["voice_id"] == "v-brian"
        assert script_of(studio, slug)["takes"]["h1"]["audio"] == f"video/{slug}/audio/vo-h1.mp3"
        assert script_of(studio, slug)["takes"]["h1"]["provider"] == "elevenlabs"
    loop.run_until_complete(go())


def test_plan_then_animatic_and_editing_a_line_needs_a_new_take(loop, studio):
    vault.set_secret("ELEVENLABS_API_KEY", "el-test-key")
    m = studio["media"]

    async def go():
        slug = await make()
        with pytest.raises(ToolError, match="isn't voiced yet"):
            await shorts.short_plan.ainvoke({"slug": slug})
        v = await shorts.short_voiceover.ainvoke({"slug": slug})  # the free scaffold voice by default
        assert v["provider"] == "local" and v["usd"] == 0 and not studio["spoken"]
        with session() as s:
            assert not s.exec(select(LedgerEntry).where(LedgerEntry.run_id == studio["run"])).all()  # nothing spent
        p = await shorts.short_plan.ainvoke({"slug": slug})
        assert set(p["cuts"]) == {"h1", "h2"}
        (spec,) = p["generate"]
        assert spec["beats"] == ["b1"] and spec["model"] == "seedance-2.5"
        assert p["generate_usd"] == round(spec["usd"], 2)
        tl = json.loads((studio["dir"] / "video" / slug / "timeline-h1.json").read_text())
        assert [v["kind"] for v in tl["video"]] == ["image", "placeholder", "clip"]
        assert tl["voice"]["src"] == f"video/{slug}/audio/vo-h1.wav" and len(tl["voice"]["segments"]) == 3
        assert tl["overlays"][0]["text"] == "every drop-in has these 5 guys" and tl["report"]["hook"] == "h1"

        r = await shorts.short_render.ainvoke({"slug": slug, "hook": "h1"})
        assert r["video"] == f"video/{slug}/{slug}-h1-animatic.mp4" and r["generate_usd"] == p["generate_usd"]
        body = m.last("/render/timeline")
        assert body["out"] == r["video"] and [v["kind"] for v in body["video"]] == ["image", "placeholder", "clip"]
        assert "Seedance 2.5" in body["video"][1]["label"] and body["captions"] and body["voice"]["segments"]
        sheet = m.last("/frames")  # the agent sees a frame from each beat of what it rendered
        assert sheet["src"] == r["video"] and sheet["labels"] == ["h1", "b1", "b2"] and r["sheet"].endswith(".png")
        with pytest.raises(ToolError, match="aren't generated yet"):
            await shorts.short_render.ainvoke({"slug": slug, "hook": "h1", "mode": "final"})
        assert any(e.text.startswith("Rendered animatic") for e in events_of(studio["run"]))

        # a new shot keeps the take; a new line doesn't
        e = await shorts.short_edit.ainvoke({"slug": slug, "part": "b2", "hold_s": 0.5,
                                             "shot": {"source": "screen", "path": "shots/rec.mp4", "speed": "fit"}})
        assert e["voice_out_of_date"] == []
        await shorts.short_plan.ainvoke({"slug": slug, "hook": "h1"})
        e = await shorts.short_edit.ainvoke({"slug": slug, "part": "b1", "vo": "Full pro gear. Zero goals."})
        assert e["voice_out_of_date"] == ["h1", "h2"]
        with pytest.raises(ToolError, match="changed since h1 was voiced"):
            await shorts.short_plan.ainvoke({"slug": slug})
        await shorts.short_voiceover.ainvoke({"slug": slug, "hooks": ["h1"]})
        p = await shorts.short_plan.ainvoke({"slug": slug, "hook": "h1"})
        assert set(p["cuts"]) == {"h1"}
    loop.run_until_complete(go())


def test_shorts_toolset_and_elevenlabs_routing(loop):
    ts = registry.all_toolsets()
    assert [t.name for t in ts["shorts"].tools] == ["short_look", "short_record", "short_stock", "short_stock_pick",
                                                    "short_new", "short_edit", "short_pace", "short_voices",
                                                    "short_voiceover", "short_plan", "short_render", "short_review"]
    assert ts["shorts"].source == "builtin" and "scaffold" in ts["shorts"].guide
    vault.set_secret("ELEVENLABS_API_KEY", "el-test-key")
    try:
        ctx = RunContext(new_run("voiceover"))
        ctx.toolsets = {"shorts": ts["shorts"]}  # type: ignore[attr-defined]
        tok = set_ctx(ctx)
        try:
            r = loop.run_until_complete(find_integrations.ainvoke({"services": ["elevenlabs", "voiceover"]}))
        finally:
            _current_ctx.reset(tok)
        assert [s["route"] for s in r["services"]] == ["toolset", "toolset"]
    finally:
        vault.delete_secret("ELEVENLABS_API_KEY")


def test_recording_steps_are_checked_and_act_like_a_visitor_never_as_the_human():
    ok = screencast.check_steps([{"wait": 1}, {"scroll": 600}, {"scroll": "bottom"}, {"scroll_to": "Saturday"},
                                 {"tap": "Games"}, {"type": "Thursday skate", "into": "Title"}, {"mark": "typed"},
                                 {"goto": "https://example.com/x"}])
    assert len(ok) == 8
    for bad in ([{"wait": 60}], [{"tap": ""}], [{"type": "x"}], [{"goto": "javascript:alert(1)"}], [{"fly": 1}],
                [{"tap": "a", "wait": 1}], [{"wait": 1}] * 41, "tap Games"):
        with pytest.raises(screencast.RecordError):
            screencast.check_steps(bad)
    site = "https://dropin-hockey.vercel.app/"
    assert screencast.may_act(site, "Games") is None
    assert screencast.may_act("http://localhost:3000/", "Organize a game") is None
    for signed_in in (False, True):
        assert "only reads it" in screencast.may_act("https://x.com/compose", "Next", signed_in=signed_in)
        assert "only reads it" in screencast.may_act("https://checkout.stripe.com/pay", "Continue", signed_in=signed_in)
        for label in ("Buy now", "Delete account", "Subscribe", "Publish", "Pay $12", "Deploy"):
            assert "never press it" in screencast.may_act(site, label, signed_in=signed_in)
    # Signed out it acts like any visitor: quiz answers with any words in them, submitting, sending a demo form
    for label in ("Submit answer", "Confirm", "Share your score", "Send", "Post",
                  "Dropout removes random neurons", "It publishes the gradients to every worker at once"):
        assert screencast.may_act("https://aiml-daily-quiz.vercel.app/", label) is None, label
    assert screencast.may_act(site, "Save", "submit") is None
    # Signed in it would act as the human: nothing that posts, sends or approves, and no submits
    for label in ("Post", "Approve", "Send", "Confirm"):
        assert "signed-in recording" in screencast.may_act(site, label, signed_in=True)
    assert "submit" in screencast.may_act(site, "Save", "submit", signed_in=True)


def test_short_record_uploads_frames_and_keeps_step_times(loop, studio, monkeypatch):
    m = studio["media"]
    seen = {}

    async def record(url, steps, device="phone", max_s=45, start_at=None, signed_in=False):
        seen["locked"] = studio_lock().locked()
        seen["start_at"], seen["signed_in"] = start_at, signed_in
        frames = [(round(k * 0.05, 3), b"\xff\xd8 jpeg %d" % k) for k in range(90)]
        return {"frames": frames, "marks": [{"t": 0.0, "step": f"open {url}"}, {"t": 1.2, "step": "tap 'Games'"}],
                "duration_s": 4.6, "width": 1080, "height": 1920}

    monkeypatch.setattr(screencast, "record", record)
    studio_lock = lambda: studio["ctx_lock"]  # noqa: E731

    async def go():
        from todd.sdk import get_agent_id, get_ctx
        studio["ctx_lock"] = get_ctx().browser_lock
        r = await shorts.short_record.ainvoke({"name": "week-view", "url": "https://dropin-hockey.vercel.app/",
                                               "steps": [{"wait": 1}, {"tap": "Games"}], "start_at": "Today"})
        assert seen["start_at"] == "Today" and seen["signed_in"] is False  # a fresh browser unless asked
        assert r["path"] == "video/recordings/week-view.mp4" and r["marks"][1] == {"t": 1.2, "step": "tap 'Games'"}
        assert seen["locked"] and not studio["ctx_lock"].locked()  # held while recording, released after
        puts = [b for p, b in m.calls if p == "/screencast/put"]
        assert [len(b["frames"]) for b in puts] == [40, 40, 10] and puts[2]["frames"][0]["i"] == 80
        asm = m.last("/screencast/assemble")
        assert asm["times"][:3] == [0.0, 0.05, 0.1] and asm["end_s"] == 4.6 and asm["session"] == puts[0]["session"]
        meta = json.loads((studio["dir"] / "video" / "recordings" / "week-view.json").read_text())
        assert meta["marks"][1]["t"] == 1.2 and meta["device"] == "phone" and meta["frames"] == 90
        assert meta["signed_in"] is False
        # the agent sees a frame just after every step, labelled
        sheet = m.last("/frames")
        assert sheet["src"] == "video/recordings/week-view.mp4" and sheet["inline"] is True
        assert sheet["times"] == [0.7, 1.9, 4.6] and sheet["labels"] == ["open", "tap", "end"]
        assert r["sheet"] == "video/recordings/week-view.png" and "contact sheet" in r["next"]
        assert get_ctx().pop_images(get_agent_id()) == [(base64.b64encode(PNG).decode(), "image/png")]
        for bad, msg in (({"name": "Week View"}, "lowercase"), ({"steps": [{"wait": 99}]}, "0–10 seconds")):
            with pytest.raises(ToolError, match=msg):
                await shorts.short_record.ainvoke({"name": "x", "url": "https://a.b/", "steps": [], **bad})

        async def refuse(url, steps, device="phone", max_s=45, start_at=None, signed_in=False):
            raise screencast.RecordError('"Buy now" looks like it buys', shot="SCREEN")
        monkeypatch.setattr(screencast, "record", refuse)
        with pytest.raises(ToolError, match="recording shop stopped: .*Buy now"):
            await shorts.short_record.ainvoke({"name": "shop", "url": "https://a.b/", "steps": [{"tap": "Buy now"}]})
        assert not studio["ctx_lock"].locked()
        assert get_ctx().pop_images(get_agent_id()) == [("SCREEN", "image/jpeg")]  # what the screen showed

        # an agent that has the browser itself (its own browser_start session) records without waiting on itself
        monkeypatch.setattr(screencast, "record", record)
        lock = get_ctx().browser_lock
        await lock.acquire(f"{get_ctx().run_id}:{get_agent_id()}")
        try:
            r = await asyncio.wait_for(shorts.short_record.ainvoke(
                {"name": "mine", "url": "https://a.b/", "steps": [{"wait": 1}]}), 5)
            assert r["path"] == "video/recordings/mine.mp4" and lock.locked()  # still that session's
        finally:
            lock.release()
        # someone else's: it says who, and gives up instead of hanging
        monkeypatch.setattr(shorts, "LOCK_WAIT_S", 0.2)
        await lock.acquire("other-run:browser")
        try:
            with pytest.raises(ToolError, match="another run is using it"):
                await shorts.short_record.ainvoke({"name": "late", "url": "https://a.b/", "steps": [{"wait": 1}]})
        finally:
            lock.release()
        assert any("Waiting for the browser (another run" in e.text for e in events_of(get_ctx().run_id))
    loop.run_until_complete(go())


def test_short_look_shows_the_page_and_its_outline(loop, studio, monkeypatch):
    seen = {}

    async def look(url, device="phone", start_at=None, signed_in=False):
        seen.update(url=url, start_at=start_at, signed_in=signed_in)
        return {"shot": "JPEG", "outline": {"url": url, "title": "NeuralDaily", "screens": 3.2, "at": 0, "items": [
            {"kind": "heading", "text": "Today's quiz", "screen": 0.1},
            {"kind": "tap", "text": "Dropout removes random neurons", "screen": 0.6},
            {"kind": "field", "text": "Your email", "screen": 2.4}]}}

    monkeypatch.setattr(screencast, "look", look)

    async def go():
        from todd.sdk import get_agent_id, get_ctx
        r = await shorts.short_look.ainvoke({"url": "https://aiml-daily-quiz.vercel.app/", "start_at": "Today"})
        assert seen == {"url": "https://aiml-daily-quiz.vercel.app/", "start_at": "Today", "signed_in": False}
        assert "3.2 screens tall" in r["outline"] and "0.6: tap: Dropout removes random neurons" in r["outline"]
        assert get_ctx().pop_images(get_agent_id()) == [("JPEG", "image/jpeg")]
        assert not get_ctx().browser_lock.locked()
    loop.run_until_complete(go())


def test_live_recording_in_the_agents_browser(loop):
    if not loop.run_until_complete(cdp.online()):
        pytest.skip("no browser reachable over CDP")

    async def go():
        rec = await screencast.record("https://example.com/", [{"wait": 0.5}, {"scroll": 300}, {"mark": "end"}])
        assert rec["frames"] and (rec["width"], rec["height"]) == (1080, 1920)
        assert [m["step"] for m in rec["marks"]][-1] == "mark end" and rec["duration_s"] > 1
    loop.run_until_complete(go())


def test_review_shows_a_free_scaffold_and_applies_feedback(loop, studio, monkeypatch):
    from todd.sdk import get_ctx
    m = studio["media"]
    answers = iter(["Slower", "Shorter hook please", "Looks good: generate it"])
    asked: list[tuple[str, dict]] = []

    async def ask(question, agent=None, data=None):
        asked.append((question, data))
        return next(answers)

    async def go():
        monkeypatch.setattr(get_ctx(), "ask_human", ask)
        slug = await make()
        await shorts.short_voiceover.ainvoke({"slug": slug})
        r1 = await shorts.short_review.ainvoke({"slug": slug})
        assert r1["version"] == 1 and not r1["approved"] and r1["applied"] == "slower"
        assert r1["video"] == f"video/{slug}/{slug}-h1-scaffold-v1.mp4"
        q, data = asked[0]
        assert "free so far" in q and "b1, b2" in q and "$" in q and data["options"][1:] == ["Slower", "Faster"]
        body = m.last("/render/timeline")
        assert body["out"] == r1["video"] and any(o["position"] == "tag" for o in body["overlays"])  # beat labels
        assert r1["pace"]["voice_speed"] < 1  # slower: the voice must be redone before planning again
        with pytest.raises(ToolError, match="voiced"):
            await shorts.short_plan.ainvoke({"slug": slug})
        await shorts.short_voiceover.ainvoke({"slug": slug})
        r2 = await shorts.short_review.ainvoke({"slug": slug})
        assert r2["version"] == 2 and r2["feedback"] == "Shorter hook please" and r2["applied"] is None
        assert "act on the feedback" in r2["next"]
        r3 = await shorts.short_review.ainvoke({"slug": slug})
        assert r3["approved"] and "elevenlabs" in r3["next"]
        reviews = script_of(studio, slug)["reviews"]
        assert [(x["version"], x["approved"]) for x in reviews] == [(1, False), (2, False), (3, True)]
        assert reviews[1]["pace"]["voice_speed"] == r1["pace"]["voice_speed"]
        p = await shorts.short_pace.ainvoke({"slug": slug, "beat_gap_s": 0.6})
        assert p["pace"]["beat_gap_s"] == 0.6 and p["next"] == "short_plan"
    loop.run_until_complete(go())


def test_a_script_follows_a_format_card_and_review_says_which(loop, studio, monkeypatch):
    from todd.db import FormatCard, session
    from todd.sdk import get_ctx
    with session() as db:
        card = FormatCard(name="question-first quiz", tags=["quiz"], card={"why": "x"},
                          examples=[{"id": "1", "plays": 50000}, {"id": "2", "plays": 90000}])
        db.add(card)
        db.commit()
        db.refresh(card)
    asked = []

    async def ask(question, agent=None, data=None):
        asked.append(question)
        return "Looks good"

    async def go():
        monkeypatch.setattr(get_ctx(), "ask_human", ask)
        with pytest.raises(ToolError, match="no format card"):
            await shorts.short_new.ainvoke({"title": "q", "hooks": HOOKS, "beats": BEATS, "format_id": "nope"})
        loose = await shorts.short_new.ainvoke({"title": "loose", "hooks": HOOKS, "beats": BEATS})
        assert any("no format card" in w for w in loose["warnings"])
        r = await shorts.short_new.ainvoke({"title": "quiz", "hooks": HOOKS, "beats": BEATS, "format_id": card.id})
        assert script_of(studio, r["slug"])["format"] == {"id": card.id, "name": "question-first quiz",
                                                         "examples": 2, "plays": [50000, 90000], "audio": None,
                                                         "measured": None}
        await shorts.short_voiceover.ainvoke({"slug": r["slug"]})
        await shorts.short_review.ainvoke({"slug": r["slug"]})
        assert "Format: question-first quiz, from 2 real video(s)." in asked[-1]
    loop.run_until_complete(go())


TEXT_HOOKS = [{"seconds": 1.6, "text": "pov: it's 11pm and you want to skate tomorrow",
               "shot": {"source": "screen", "path": "shots/rec.mp4", "focus": {"x": 0.5, "y": 0.3, "zoom": 1.4}}}]
TEXT_BEATS = [{"seconds": 2.2, "text": "the rec site has it on 4 pages", "shots": [
                  {"source": "screen", "path": "shots/rec.mp4", "in_s": 0.5},
                  {"source": "screen", "path": "shots/rec.mp4", "in_s": 2.5}]},
              {"seconds": 2.0, "text": "this one has all of it", "text_style": "outline",
               "shot": {"source": "image", "path": "shots/bench.png"}}]


def test_a_text_and_sound_short_needs_no_voice(loop, studio, monkeypatch):
    from todd.sdk import get_ctx
    asked = []

    async def ask(question, agent=None, data=None):
        asked.append(question)
        return "Looks good"

    async def go():
        monkeypatch.setattr(get_ctx(), "ask_human", ask)
        with pytest.raises(ToolError, match="give it seconds"):
            await shorts.short_new.ainvoke({"title": "x", "hooks": [{"text": "hi", "shot": TEXT_BEATS[1]["shot"]}],
                                            "beats": TEXT_BEATS})
        r = await shorts.short_new.ainvoke({"title": "rink pov", "hooks": TEXT_HOOKS, "beats": TEXT_BEATS,
                                            "sound": "trending sound: a sped-up pop song (add in the app)"})
        assert r["estimate_s"] == pytest.approx(5.8) and "nothing to voice" in r["next"]
        assert any("add the sound in the app" in w for w in r["warnings"])
        s = script_of(studio, r["slug"])
        assert s["captions"] is None and s["hooks"][0]["seconds"] == 1.6 and len(s["beats"][0]["shots"]) == 2
        v = await shorts.short_voiceover.ainvoke({"slug": r["slug"]})
        assert v["takes"] == {} and not studio["spoken"]
        plan = await shorts.short_plan.ainvoke({"slug": r["slug"]})
        assert plan["cuts"]["h1"]["duration_s"] == pytest.approx(5.8) and plan["cuts"]["h1"]["shots"] == 4
        await shorts.short_render.ainvoke({"slug": r["slug"], "hook": "h1"})
        tl = studio["media"].last("/render/timeline")
        assert "voice" not in tl and tl["captions"] == [] and tl["video"][0]["crop"]["zoom"] == 1.4
        assert [(o["text"], o["style"]) for o in tl["overlays"] if o["position"] != "tag"] == [
            ("pov: it's 11pm and you want to skate tomorrow", "box"), ("the rink site has it on 4 pages".replace(
                "rink", "rec"), "box"), ("this one has all of it", "outline")]
        rv = await shorts.short_review.ainvoke({"slug": r["slug"]})
        assert rv["approved"] and "No voice" in asked[-1] and "Sound: trending sound" in asked[-1]
        assert "short_render(mode=\"final\")" in rv["next"] and "elevenlabs" not in rv["next"]
        # a voiced short can turn a beat silent (a reveal), and back
        e = await shorts.short_edit.ainvoke({"slug": r["slug"], "part": "b2", "vo": "And it's all on one page."})
        assert e["changed"] == ["vo"] and script_of(studio, r["slug"])["beats"][1]["vo"].startswith("And it's")
        with pytest.raises(ToolError, match="isn't voiced yet"):
            await shorts.short_plan.ainvoke({"slug": r["slug"]})
        await shorts.short_voiceover.ainvoke({"slug": r["slug"]})
        plan = await shorts.short_plan.ainvoke({"slug": r["slug"]})
        tl = (await shorts.short_render.ainvoke({"slug": r["slug"], "hook": "h1"}))
        sent = studio["media"].last("/render/timeline")
        assert len(sent["voice"]["segments"]) == 1 and sent["voice"]["segments"][0]["at"] == pytest.approx(3.8)
        assert sent["captions"] and sent["caption_style"] == "phrase"
    loop.run_until_complete(go())


def test_stock_footage_is_searched_by_eye_downloaded_and_credited(loop, studio, monkeypatch):
    from todd.sdk import get_agent_id, get_ctx
    from todd.tools import stock
    asked, got = [], []

    async def ask(question, agent=None, data=None):
        asked.append((question, data))
        return "skip"

    hits = [{"id": 11, "pageURL": "https://pixabay.com/videos/rink-11/", "tags": "ice, rink", "duration": 14,
             "user": "skater", "user_id": 5, "videos": {
                 "large": {"url": "https://cdn.pixabay.com/video/11_large.mp4", "width": 1920, "height": 1080,
                           "thumbnail": "https://cdn.pixabay.com/video/11_large.jpg"},
                 "medium": {"url": "https://cdn.pixabay.com/video/11_medium.mp4", "width": 1280, "height": 720,
                            "thumbnail": "https://cdn.pixabay.com/video/11_medium.jpg"}}},
            {"id": 12, "pageURL": "https://pixabay.com/videos/laces-12/", "tags": "skates", "duration": 9,
             "user": "puck", "user_id": 6, "videos": {
                 "large": {"url": "https://cdn.pixabay.com/video/12_large.mp4", "width": 1080, "height": 1920,
                           "thumbnail": "https://cdn.pixabay.com/video/12_large.jpg"}}}]

    async def fake_get(url, params, headers=None):
        got.append((url, params))
        if params.get("id"):
            return {"hits": [{**next(h for h in hits if str(h["id"]) == params["id"]), "videos": {
                "large": {"url": "https://cdn.pixabay.com/video/12_fresh.mp4", "width": 1080, "height": 1920}}}]}
        return {"hits": hits[:1] if params.get("q") == "empty rink" else hits}

    monkeypatch.setattr(stock, "_get", fake_get)
    stock._cache.clear()

    async def go():
        monkeypatch.setattr(get_ctx(), "ask_human", ask)
        with pytest.raises(ToolError, match="didn't add PIXABAY_API_KEY"):
            await stock.short_stock.ainvoke({"query": "hands lacing skates"})
        assert asked[0][1] == {"secret_name": "PIXABAY_API_KEY"} and "pixabay.com/api/docs" in asked[0][0]
        vault.set_secret("PIXABAY_API_KEY", "px-key")
        try:
            r = await stock.short_stock.ainvoke({"query": "hands lacing skates", "k": 4})
            # another producer's search in between doesn't change what "1" means for this one
            from todd.runtime import _current_agent, set_current_agent
            tok = set_current_agent("other-producer")
            await stock.short_stock.ainvoke({"query": "empty rink", "k": 4})
            get_ctx().pop_images("other-producer")
            _current_agent.reset(tok)
            assert [c["key"] for c in r["candidates"]] == ["pixabay:12", "pixabay:11"]  # portrait first
            assert r["candidates"][0]["portrait"] and got[0][1]["q"] == "hands lacing skates"
            assert get_ctx().pop_images(get_agent_id()) == [(base64.b64encode(b"thumbs").decode(), "image/png")]
            p = await stock.short_stock_pick.ainvoke({"pick": "1", "name": "laces"})
            assert p["path"] == "video/stock/laces.mp4" and p["duration_s"] == 12.0
            assert studio["media"].last("/fetch/video")["url"] == "https://cdn.pixabay.com/video/12_fresh.mp4"
            credits = (studio["dir"] / "video" / "stock" / "CREDITS.md").read_text()
            assert "video/stock/laces.mp4: video by [puck](https://pixabay.com/users/puck-6/)" in credits
            # a stock clip in a short is toned down to sit with phone footage
            beats = [{"vo": "Skates on.", "shot": {"source": "clip", "path": p["path"], "in_s": 4}}]
            slug = (await shorts.short_new.ainvoke({"title": "laces", "hooks": HOOKS[:1], "beats": beats}))["slug"]
            await shorts.short_voiceover.ainvoke({"slug": slug})
            await shorts.short_render.ainvoke({"slug": slug, "hook": "h1"})
            clip = studio["media"].last("/render/timeline")["video"][-1]
            assert clip["src"] == "video/stock/laces.mp4" and clip["grade"] == "phone" and clip["in_s"] == 4
        finally:
            vault.delete_secret("PIXABAY_API_KEY")
    loop.run_until_complete(go())


def test_a_silent_hook_variant_next_to_a_spoken_one(loop, studio):
    async def go():
        hooks = [HOOKS[0], {"seconds": 1.5, "text": "pov: drop-in", "shot": {"source": "image",
                                                                           "path": "shots/bench.png"}}]
        beats = [{"seconds": 2, "text": "every week", "shot": {"source": "screen", "path": "shots/rec.mp4"}}]
        slug = (await shorts.short_new.ainvoke({"title": "mixed", "hooks": hooks, "beats": beats}))["slug"]
        v = await shorts.short_voiceover.ainvoke({"slug": slug})
        assert list(v["takes"]) == ["h1"]  # h2 says nothing: no take, and no empty request
        plan = await shorts.short_plan.ainvoke({"slug": slug})
        assert set(plan["cuts"]) == {"h1", "h2"} and plan["cuts"]["h2"]["duration_s"] == pytest.approx(3.5)
        r = await shorts.short_render.ainvoke({"slug": slug, "hook": "h2"})
        assert "voice" not in studio["media"].last("/render/timeline") and r["duration_s"] == pytest.approx(3.5)
    loop.run_until_complete(go())
