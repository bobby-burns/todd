"""The shorts toolset end to end: script → voiceover (one charge) → plan → animatic. The real sandbox server on a temp
folder, a fake media service and a fake ElevenLabs that returns realistic character timestamps: no network, no
FFmpeg."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from todd import registry, vault
from todd.config import config
from todd.db import LedgerEntry, select, session
from todd.integrations import find_integrations
from todd.runtime import RunContext
from todd.sdk import ToolError, _current_ctx, set_ctx
from todd.tools import elevenlabs, media, sandbox, shorts

from .helpers import events_of, new_run

SERVER = Path(__file__).resolve().parents[2] / "sandbox" / "server.py"


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
        if path == "/render/timeline":
            out = run / payload["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00\x00\x00\x20ftypisom placeholder")
            frames = sum(v["frames"] for v in payload["video"])
            return {"path": payload["out"], "duration_s": round(frames / payload["fps"], 3), "frames": frames,
                    "size_bytes": out.stat().st_size}
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


def test_voiceover_needs_a_key_then_voices_every_hook_for_one_charge(loop, studio):
    async def go():
        slug = await make()
        with pytest.raises(ToolError, match="elevenlabs.io/app/settings/api-keys"):
            await shorts.short_voiceover.ainvoke({"slug": slug})
        vault.set_secret("ELEVENLABS_API_KEY", "el-test-key")
        r = await shorts.short_voiceover.ainvoke({"slug": slug})
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
    loop.run_until_complete(go())


def test_plan_then_animatic_and_editing_a_line_needs_a_new_take(loop, studio):
    vault.set_secret("ELEVENLABS_API_KEY", "el-test-key")
    m = studio["media"]

    async def go():
        slug = await make()
        with pytest.raises(ToolError, match="isn't voiced yet"):
            await shorts.short_plan.ainvoke({"slug": slug})
        await shorts.short_voiceover.ainvoke({"slug": slug})
        p = await shorts.short_plan.ainvoke({"slug": slug})
        assert set(p["cuts"]) == {"h1", "h2"}
        (spec,) = p["generate"]
        assert spec["beats"] == ["b1"] and spec["model"] == "seedance-2.5"
        assert p["generate_usd"] == round(spec["usd"], 2)
        tl = json.loads((studio["dir"] / "video" / slug / "timeline-h1.json").read_text())
        assert [v["kind"] for v in tl["video"]] == ["image", "placeholder", "clip"]
        assert tl["voice"]["src"] == f"video/{slug}/audio/vo-h1.mp3" and len(tl["voice"]["segments"]) == 3
        assert tl["overlays"][0]["text"] == "every drop-in has these 5 guys" and tl["report"]["hook"] == "h1"

        r = await shorts.short_render.ainvoke({"slug": slug, "hook": "h1"})
        assert r["video"] == f"video/{slug}/{slug}-h1-animatic.mp4" and r["generate_usd"] == p["generate_usd"]
        body = m.last("/render/timeline")
        assert body["out"] == r["video"] and [v["kind"] for v in body["video"]] == ["image", "placeholder", "clip"]
        assert "Seedance 2.5" in body["video"][1]["label"] and body["captions"] and body["voice"]["segments"]
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
    assert [t.name for t in ts["shorts"].tools] == ["short_new", "short_edit", "short_voices", "short_voiceover",
                                                    "short_plan", "short_render"]
    assert ts["shorts"].source == "builtin" and "animatic" in ts["shorts"].guide
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
