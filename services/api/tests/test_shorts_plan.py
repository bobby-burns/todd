"""The shorts timing planner: the voiceover take is the master clock. Synthetic takes with known word times, so every
cut, voice segment, caption page and clip length can be checked by hand."""

from __future__ import annotations

import pytest

from todd import shorts_plan as sp

FPS = 30


def script(**kw) -> dict:
    base = {
        "slug": "demo", "platform": "tiktok", "width": 1080, "height": 1920, "end_hold_s": 0.6,
        "pace": {"beat_gap_s": 0, "min_shot_s": 0.6},  # the timing tests below check the take's own pauses
        "hooks": [{"id": "h1", "vo": "Every drop-in has five guys.", "text": "5 guys",
                   "shot": {"source": "image", "path": "shots/bench.png"}},
                  {"id": "h2", "vo": "Rating every guy at drop-in.",
                   "shot": {"source": "image", "path": "shots/b.png"}}],
        "beats": [{"id": "b1", "vo": "Full pro gear.", "text": "1. pro gear",
                   "shot": {"source": "ai", "prompt": "a player in full pro gear", "model": "seedance-2.5"}},
                  {"id": "b2", "vo": "And the one who checked first.",
                   "shot": {"source": "screen", "path": "shots/rec.mp4"}}],
    }
    base.update(kw)
    return base


def take(word_times: list[tuple[str, float, float]], text: str | None = None) -> dict:
    """An ElevenLabs-style character alignment for words at the given times (spaces between them)."""
    chars, starts, ends = [], [], []
    for i, (w, a, b) in enumerate(word_times):
        if i:
            chars.append(" ")
            starts.append(word_times[i - 1][2])
            ends.append(a)
        step = (b - a) / max(len(w), 1)
        for j, ch in enumerate(w):
            chars.append(ch)
            starts.append(a + j * step)
            ends.append(a + (j + 1) * step)
    return {"characters": chars, "character_start_times_seconds": starts, "character_end_times_seconds": ends}


H1 = [("Every", 0.40, 0.70), ("drop-in", 0.72, 1.10), ("has", 1.12, 1.25), ("five", 1.27, 1.50),
      ("guys.", 1.52, 1.90),
      ("Full", 2.30, 2.50), ("pro", 2.52, 2.70), ("gear.", 2.72, 3.10),
      ("And", 3.50, 3.62), ("the", 3.64, 3.72), ("one", 3.74, 3.90), ("who", 3.92, 4.05), ("checked", 4.07, 4.45),
      ("first.", 4.47, 4.90)]
MEDIA = {"shots/bench.png": {"kind": "image"}, "shots/b.png": {"kind": "image"},
         "shots/rec.mp4": {"kind": "video", "duration_s": 6.0}}


def run(s=None, t=None, media=None, hook="h1"):
    return sp.plan(s or script(), hook, take(t or H1), media or MEDIA, "audio/vo-h1.mp3")


def test_words_belong_to_their_beats_even_when_the_alignment_differs_slightly():
    s = script()
    text, spans = sp.take_text(s, "h1")
    assert text == "Every drop-in has five guys. Full pro gear. And the one who checked first."
    ws = sp.words(text, spans, take(H1))
    assert [(w.text, w.beat) for w in ws][:6] == [("Every", 0), ("drop-in", 0), ("has", 0), ("five", 0),
                                                 ("guys.", 0), ("Full", 1)]
    assert [w.beat for w in ws][-1] == 2
    # the service normalised "drop-in" to "drop in" and doubled a space: words still land in the right beats
    odd = take([("Every", 0.4, 0.7), ("drop", 0.72, 0.9), ("in", 0.9, 1.1), ("has", 1.12, 1.25), ("five", 1.27, 1.5),
                ("guys.", 1.52, 1.9), ("", 1.9, 1.95), ("Full", 2.3, 2.5), ("pro", 2.52, 2.7), ("gear.", 2.72, 3.1),
                ("And", 3.5, 3.62), ("the", 3.64, 3.72), ("one", 3.74, 3.9), ("who", 3.92, 4.05),
                ("checked", 4.07, 4.45), ("first.", 4.47, 4.9)])
    ws = sp.words(text, spans, odd)
    assert {w.text: w.beat for w in ws}["Full"] == 1 and {w.text: w.beat for w in ws}["And"] == 2


def test_cuts_land_two_frames_before_each_line_and_the_hook_starts_at_once():
    p = run()
    tl = p.timeline
    lead = 0.40 - sp.PRE_ROLL  # leading silence trimmed
    assert tl["voice"]["segments"][0] == {"src_in": round(lead, 4), "src_out": 2.1, "at": 0.0}
    cut1 = round((2.30 - lead) * FPS) - 2
    cut2 = round((3.50 - lead) * FPS) - 2
    total = round((4.90 - lead + 0.6) * FPS)
    assert [v["frames"] for v in tl["video"]] == [cut1, cut2 - cut1, total - cut2]
    assert tl["frames"] == total and tl["duration_s"] == pytest.approx(total / FPS, abs=1e-3)
    # each segment is placed where its words were: the voice is never moved relative to the picture
    seg1 = tl["voice"]["segments"][1]
    assert seg1["src_in"] == pytest.approx((1.90 + 2.30) / 2) and seg1["at"] == pytest.approx(seg1["src_in"] - lead)
    assert tl["voice"]["segments"][2]["src_out"] == pytest.approx(4.90 + sp.TAIL)
    assert tl["overlays"][0] == {"text": "5 guys", "start": 0.0, "end": round(cut1 / FPS, 3), "position": "top",
                               "style": "box"}


def test_a_hold_adds_silence_between_lines_and_moves_everything_after_it():
    s = script()
    s["beats"][0]["hold_s"] = 0.8  # a visual payoff after "Full pro gear."
    p = run(s)
    base = run()
    seg_b, seg_h = base.timeline["voice"]["segments"], p.timeline["voice"]["segments"]
    assert seg_h[:2] == seg_b[:2]  # nothing before the hold moves
    assert seg_h[2]["src_in"] == seg_b[2]["src_in"] and seg_h[2]["at"] == pytest.approx(seg_b[2]["at"] + 0.8)
    fb, fh = [v["frames"] for v in base.timeline["video"]], [v["frames"] for v in p.timeline["video"]]
    assert fh[0] == fb[0] and fh[1] == fb[1] + 24 and fh[2] == fb[2]  # the held beat is 0.8 s (24 frames) longer
    assert p.timeline["frames"] == base.timeline["frames"] + 24


def test_a_shot_can_pin_its_moment_to_a_word():
    s = script()
    s["beats"][1]["shot"]["sync"] = {"word": "checked", "at_s": 3.2}  # the tap happens 3.2 s into the recording
    p = run(s)
    item = p.timeline["video"][2]
    lead = 0.40 - sp.PRE_ROLL
    slot_start = sum(v["frames"] for v in p.timeline["video"][:2]) / FPS
    spoken = 4.07 - lead  # "checked" on the timeline
    assert item["in_s"] == pytest.approx(3.2 - (spoken - slot_start), abs=1e-3)
    # the recording's 3.2 s mark is on screen exactly when "checked" is said
    assert item["in_s"] + (spoken - slot_start) * item["speed"] == pytest.approx(3.2, abs=1e-3)
    # a moment too early in the recording: the page's first frame is held until it can play into the word
    s["beats"][1]["shot"]["sync"] = {"word": "checked", "at_s": 0.3}
    p = run(s)
    item = p.timeline["video"][2]
    assert item["in_s"] == 0 and item["hold_s"] == pytest.approx((spoken - slot_start) - 0.3, abs=1e-3)
    assert item["hold_s"] + 0.3 == pytest.approx(spoken - slot_start, abs=1e-3)  # 0.3 s plays, then the word
    assert not [w for w in p.report["warnings"] if "sync" in w or "too short" in w]
    assert p.report["beats"][2]["fit"].startswith("holds its first frame")
    s["beats"][1]["shot"]["sync"] = {"word": "nope", "at_s": 1}
    assert any("isn't in this beat" in w for w in run(s).report["warnings"])


@pytest.mark.parametrize("dur,speed,expect,source", [
    (6.0, 1, "trim", "screen"),
    (1.9, 1, "slowed to", "screen"),  # the slot is 2.07 s: 0.92× is barely visible
    (1.65, 1, "holds its last frame", "ai"),  # 0.80× would show; a 0.42 s hold doesn't
    (0.5, 1, "holds its last frame", "screen"),  # a still page for 1.57 s reads as the page waiting: no re-record
    (0.5, 1, "too short by", "ai"),  # a generated clip frozen that long looks broken
    (8.0, "fit", "sped up", "screen"),
])
def test_how_a_recording_fills_its_slot(dur, speed, expect, source):
    s = script()
    s["beats"][1]["shot"]["speed"] = speed
    if source == "ai":
        s["beats"][1]["shot"] = {"source": "ai", "prompt": "x", "clip": "shots/rec.mp4", "speed": speed}
    p = run(s, media={**MEDIA, "shots/rec.mp4": {"kind": "video", "duration_s": dur}})
    beat = p.report["beats"][2]
    assert beat["fit"].startswith(expect), beat
    item = p.timeline["video"][2]
    covered = (dur - item["in_s"]) / item["speed"] + item["freeze_s"]
    assert covered >= beat["slot_s"] - 1e-3  # the slot is always filled
    assert ("too short" in " ".join(p.report["warnings"])) == (expect == "too short by")


def test_ai_shots_are_placeholders_with_an_exact_price_until_generated():
    p = run()
    b1 = p.timeline["video"][1]
    assert b1["kind"] == "placeholder" and "Seedance 2.5" in b1["label"] and "pro gear" in b1["label"]
    (spec,) = p.generate
    slot = p.report["beats"][1]["slot_s"]
    assert spec["seconds"] == max(2, int(-(-(slot + sp.HANDLE_S) // 1)))  # the shortest length covering slot + handle
    assert spec["usd"] == pytest.approx(spec["seconds"] * 0.0738, abs=1e-3)
    assert p.report["estimate_usd"] == round(spec["usd"], 2)
    s = script()
    s["beats"][0]["shot"]["model"] = "kling-3.0"
    assert run(s).generate[0]["seconds"] == 5  # Kling makes 5 or 10 s clips
    # once generated, the clip is used like any recording
    s["beats"][0]["shot"]["clip"] = "shots/b1.mp4"
    p = run(s, media={**MEDIA, "shots/b1.mp4": {"kind": "video", "duration_s": 5.0}})
    assert p.timeline["video"][1]["kind"] == "clip" and not p.generate


def test_one_generation_spec_per_shot_covers_every_hook_variant():
    long_hook = [("Rating", 0.3, 0.6), ("every", 0.62, 0.9), ("guy", 0.92, 1.1), ("at", 1.12, 1.2),
                 ("drop-in.", 1.22, 1.7)] + [(w, a - 0.2, b - 0.2) for w, a, b in H1[5:]]
    stretched = [(w, a, b + (0.6 if w == "gear." else 0)) for w, a, b in long_hook]
    p1 = run()
    p2 = sp.plan(script(), "h2", take([(w, a, b if w != "gear." else b) for w, a, b in stretched]), MEDIA, "x.mp3")
    merged = sp.merge_generation([p1, p2])
    assert len(merged) == 1 and merged[0]["beats"] == ["b1"]
    assert merged[0]["slot_s"] == max(p1.generate[0]["slot_s"], p2.generate[0]["slot_s"])
    # the same AI shot in two hook variants is generated once, long enough for both
    s = script()
    same = {"source": "ai", "prompt": "skates on a bench", "start_image": "shots/bench.png"}
    s["hooks"][0]["shot"] = s["hooks"][1]["shot"] = same
    a = sp.plan(s, "h1", take(H1), MEDIA, "x.mp3")
    b = sp.plan(s, "h2", take(long_hook), MEDIA, "y.mp3")
    merged = sp.merge_generation([a, b])
    hooks = next(g for g in merged if g["prompt"] == "skates on a bench")
    assert hooks["beats"] == ["h1", "h2"] and len(merged) == 2
    assert hooks["slot_s"] == max(a.report["beats"][0]["slot_s"], b.report["beats"][0]["slot_s"])


def test_captions_follow_the_voice_and_never_cross_a_beat():
    p = run()
    pages = p.timeline["captions"]
    texts = [" ".join(w["text"] for w in pg["words"]) for pg in pages]
    assert texts == ["Every drop-in has", "five guys", "Full pro gear", "And the one", "who checked first"]
    cuts = []
    acc = 0
    for v in p.timeline["video"]:
        acc += v["frames"]
        cuts.append(acc / FPS)
    for pg in pages:
        beat_end = next(c for c in cuts if c > pg["start"])
        assert pg["end"] <= beat_end + 1e-6  # a page ends before the next beat's cut
        assert pg["words"][0]["start"] == pg["start"] and all(w["end"] <= pg["end"] + 0.31 for w in pg["words"])
    fast = [(w, a, a + 0.05) for w, a, _ in H1[:5]] + H1[5:]
    fast = [(w, 0.4 + i * 0.06, 0.4 + i * 0.06 + 0.05) if i < 5 else (w, a, b) for i, (w, a, b) in enumerate(fast)]
    assert any("on screen only" in w for w in run(t=fast).report["warnings"])


def test_platform_length_and_estimates():
    long_t = [(w, a * 6, b * 6) for w, a, b in H1]  # the same script read very slowly: ~29 s
    rep = run(t=long_t).report
    assert any("past the 7–20s sweet spot" in w for w in rep["warnings"])
    rep = run(t=[(w, a * 7, b * 7) for w, a, b in H1]).report  # ~34 s: a cut to make, whatever the platform allows
    assert any("too long: cut lines until it's under 30s" in w for w in rep["warnings"])
    assert sp.length_notes(12, sp.PLATFORMS["tiktok"]) == []
    assert "ends before it lands" in sp.length_notes(5, sp.PLATFORMS["reels"])[0]
    assert 4 < sp.estimate_seconds(script()) < 8
    with pytest.raises(ValueError, match="no hook"):
        sp.take_text(script(), "h9")
    bad = take([("Every", 0.4, 0.7)])  # a take that stops after one word
    with pytest.raises(ValueError, match="no words found"):
        sp.plan(script(), "h1", bad, MEDIA, "x.mp3")


def test_pace_adds_breathing_room_and_holds_short_shots():
    base = run()
    s = script(pace={"beat_gap_s": 0.4, "min_shot_s": 0.6})
    gap = sp.plan(s, "h1", take(H1), MEDIA, "x.mp3")
    segs_b, segs_g = base.timeline["voice"]["segments"], gap.timeline["voice"]["segments"]
    # every later line starts 0.4 s later per gap before it; the voice itself isn't touched
    assert segs_g[1]["at"] == pytest.approx(segs_b[1]["at"] + 0.4)
    assert segs_g[2]["at"] == pytest.approx(segs_b[2]["at"] + 0.8)
    assert [g["src_in"] for g in segs_g] == [b["src_in"] for b in segs_b]
    assert gap.timeline["frames"] == base.timeline["frames"] + 24  # two gaps of 0.4 s (none after the last line)
    # a short line is held until it has had min_shot_s on screen
    held = sp.plan(script(pace={"beat_gap_s": 0, "min_shot_s": 2.0}), "h1", take(H1), MEDIA, "x.mp3")
    slots = [b["slot_s"] for b in held.report["beats"]]
    assert all(x >= 2.0 - 1 / 30 for x in slots) and slots[1] > [b["slot_s"] for b in base.report["beats"]][1]
    assert held.report["pace"]["min_shot_s"] == 2.0


def test_caption_size_presets_and_limits():
    pages = run(script(pace={"beat_gap_s": 0, "min_shot_s": 0.6, "caption_words": 1})).timeline["captions"]
    assert all(len(pg["words"]) == 1 for pg in pages)
    base = sp.pace({})
    assert base == sp.DEFAULT_PACE
    slower = sp.pace({}, preset="slower")
    assert slower["voice_speed"] < 1 and slower["beat_gap_s"] > base["beat_gap_s"] and slower["caption_words"] == 2
    assert slower["min_shot_s"] > base["min_shot_s"]
    faster = sp.pace({"pace": {"voice_speed": 1.2, "beat_gap_s": 0}}, preset="faster")
    assert faster["voice_speed"] == 1.2 and faster["beat_gap_s"] == 0  # clamped, never past the limits
    assert sp.pace({}, change={"caption_words": 9, "voice_speed": 0.1}) == {**base, "caption_words": 4,
                                                                           "voice_speed": 0.8}
    tags = run().timeline["tags"]
    assert [t["text"].split(" · ")[0] for t in tags] == ["h1", "b1", "b2"] and tags[0]["position"] == "tag"


def test_emoji_are_left_out_of_on_screen_text():
    s = script()
    s["beats"][0]["text"] = "1. pro gear 🔥🤔"
    s["hooks"][0]["text"] = "🧠"
    p = sp.plan(s, "h1", take(H1), MEDIA, "x.mp3")
    texts = [o["text"] for o in p.timeline["overlays"]]
    assert "1. pro gear" in texts and not any("🔥" in t or "🧠" in t for t in texts)
    assert "🧠" not in texts and len(texts) == 1  # an emoji-only text box is dropped
    assert any("emoji left out" in w for w in p.report["warnings"])
    assert sp.plain("TRUE ✅ ok") == "TRUE ok"


def test_a_text_and_sound_short_is_timed_by_its_beats_seconds():
    s = script(captions="none", hooks=[{"id": "h1", "vo": "", "seconds": 1.5, "text": "pov: it's tuesday",
                                        "shot": {"source": "screen", "path": "shots/rec.mp4"}}],
               beats=[{"id": "b1", "vo": "", "seconds": 2.0, "text": "the rink schedule is on 4 pages",
                       "shot": {"source": "screen", "path": "shots/rec.mp4", "in_s": 1}},
                      {"id": "b2", "vo": "", "seconds": 0.8, "text": "one site has all of it, on your phone, finally",
                       "shot": {"source": "image", "path": "shots/b.png"}}])
    p = sp.plan(s, "h1", None, MEDIA, None)
    tl = p.timeline
    assert tl["voice"] is None and tl["captions"] == []
    assert [v["frames"] for v in tl["video"]] == [45, 60, 24] and tl["frames"] == 129  # 1.5 + 2 + 0.8 s, no end hold
    assert [(o["text"], o["start"], o["end"]) for o in tl["overlays"]] == [
        ("pov: it's tuesday", 0.0, 1.5), ("the rink schedule is on 4 pages", 1.5, 3.5),
        ("one site has all of it, on your phone, finally", 3.5, 4.3)]
    assert any("isn't enough to read" in w for w in p.report["warnings"])  # 9 words in 0.8 s
    assert sp.estimate_seconds(s) == pytest.approx(4.3)
    with pytest.raises(ValueError, match="needs seconds"):
        sp.plan({**s, "beats": [{"id": "b1", "vo": "", "shot": {"source": "image", "path": "shots/b.png"}}]}, "h1",
                None, MEDIA, None)


def test_silent_beats_sit_between_the_lines_without_moving_the_voice_against_its_picture():
    base = run()
    s = script()
    s["hooks"][0] = {"id": "h1", "vo": "", "seconds": 1.0, "text": "wait for it",
                     "shot": {"source": "image", "path": "shots/bench.png"}}
    s["beats"].insert(1, {"id": "b2", "vo": "", "seconds": 0.9, "shot": {"source": "image", "path": "shots/b.png"}})
    s["beats"][2]["id"] = "b3"
    s["beats"].append({"id": "b4", "vo": "", "seconds": 1.2, "text": "that's it",
                       "shot": {"source": "image", "path": "shots/b.png"}})
    # the take is just the spoken lines: "Full pro gear." and "And the one who checked first."
    t = [(w, a - 2.3 + 0.4, b - 2.3 + 0.4) for w, a, b in H1[5:]]
    p = sp.plan(s, "h1", take(t), MEDIA, "x.mp3")
    tl = p.timeline
    ids = [b["id"] for b in p.report["beats"]]
    assert ids == ["h1", "b1", "b2", "b3", "b4"]
    slots = {b["id"]: (b["start_s"], b["end_s"]) for b in p.report["beats"]}
    assert slots["h1"] == (0.0, 1.0)  # the silent hook, then the first line starts
    assert slots["b2"][1] - slots["b2"][0] == pytest.approx(0.9, abs=1 / FPS)
    assert slots["b4"][1] - slots["b4"][0] == pytest.approx(1.2, abs=1 / FPS) and slots["b4"][1] == tl["duration_s"]
    segs = tl["voice"]["segments"]
    assert len(segs) == 2 and segs[0]["at"] == pytest.approx(1.0)  # the voice waits for the silent hook
    # the second line is pushed back by the silent beat before it, and is still on its own picture
    lead = 0.4 - sp.PRE_ROLL
    first_word_b3 = 3.5 - 2.3 + 0.4
    assert slots["b3"][0] == pytest.approx(first_word_b3 - lead + 1.0 + 0.9 - 2 / FPS, abs=1.5 / FPS)
    for pg in tl["captions"]:  # no caption runs into a silent beat
        assert not (slots["b2"][0] < pg["start"] < slots["b2"][1]) and pg["end"] <= slots["b4"][0] + 1e-6
    assert [o["text"] for o in tl["overlays"]] == ["wait for it", "1. pro gear", "that's it"]
    assert base.timeline["voice"]["segments"][0]["at"] == 0.0


def test_quick_cuts_split_a_beat_and_a_punch_in_lands_on_its_moment():
    s = script()
    s["beats"][1] = {"id": "b2", "vo": "And the one who checked first.", "shots": [
        {"source": "screen", "path": "shots/rec.mp4", "in_s": 0.5, "text": "the one who checked"},
        {"source": "screen", "path": "shots/rec.mp4", "in_s": 2.0, "share": 2,
         "focus": {"x": 0.5, "y": 0.3, "zoom": 1.6, "at_s": 2.6}}]}
    p = run(s)
    tl = p.timeline
    beat = p.report["beats"][2]
    items = tl["video"][2:]
    assert len(items) == 3  # the first cut, then the second cut before and after its punch-in
    total = sum(v["frames"] for v in items)
    assert items[0]["frames"] == round(total / 3) and "crop" not in items[0]
    assert "crop" not in items[1] and items[2]["crop"] == {"x": 0.5, "y": 0.3, "zoom": 1.6}
    assert items[1]["frames"] == round(0.6 * FPS) and items[2]["in_s"] == 2.6  # punched in 0.6 s into the cut
    assert "b2.2" in beat["fit"] and "punches in 1.6" in beat["fit"]
    texts = [(o["text"], o["end"]) for o in tl["overlays"]]
    assert ("the one who checked", round(beat["start_s"] + items[0]["frames"] / FPS, 3)) in texts
    assert p.report["cuts"] == 4  # five pieces of video on the timeline
    # a whole cut punched in, and one long unchanging shot is flagged
    s["beats"][1]["shots"] = [{"source": "screen", "path": "shots/rec.mp4", "focus": {"x": 0.2, "y": 0.8, "zoom": 1.2}}]
    item = run(s).timeline["video"][-1]
    assert item["crop"] == {"x": 0.2, "y": 0.8, "zoom": 1.2}
    slow = [(w, a * 3, b * 3) for w, a, b in H1]
    assert any("stays on one shot" in w for w in run(s, t=slow).report["warnings"])


def test_stock_and_generated_clips_are_toned_down_and_screens_are_not():
    s = script()
    s["beats"][1]["shot"] = {"source": "clip", "path": "video/stock/rink.mp4"}
    s["hooks"][0]["shot"] = {"source": "screen", "path": "shots/rec.mp4"}
    p = run(s, media={**MEDIA, "video/stock/rink.mp4": {"kind": "video", "duration_s": 9.0}})
    assert p.timeline["video"][2]["grade"] == "phone" and "grade" not in p.timeline["video"][0]


def test_a_punch_in_past_the_end_of_its_clip_doesnt_cut_into_nothing():
    s = script()
    s["beats"][1]["shot"] = {"source": "screen", "path": "shots/rec.mp4", "focus": {"x": 0.5, "y": 0.5, "zoom": 1.5,
                                                                                   "at_s": 9.0}}
    p = run(s, media={**MEDIA, "shots/rec.mp4": {"kind": "video", "duration_s": 1.0}})
    items = p.timeline["video"][2:]
    assert len(items) == 1 and items[0]["crop"]["zoom"] == 1.5  # punched in from the start instead
    assert any("past the end" in w for w in p.report["warnings"])
    assert sum(v["frames"] for v in p.timeline["video"]) == p.timeline["frames"]


def test_too_many_cuts_for_a_beat_is_an_error_not_a_bad_render():
    with pytest.raises(ValueError, match="cuts don't fit"):
        sp._split(2, [{}, {}, {}])
    assert sp._split(7, [{}, {}, {"share": 5}]) == [1, 1, 5]


def test_one_hook_spoken_and_another_silent_plan_from_their_own_takes():
    s = script(beats=[{"id": "b1", "vo": "", "seconds": 1.5, "shot": {"source": "image", "path": "shots/b.png"}}])
    s["hooks"][1] = {"id": "h2", "vo": "", "seconds": 2.0, "text": "rating every guy",
                     "shot": {"source": "image", "path": "shots/b.png"}}
    assert sp.take_text(s, "h2")[0] == ""
    silent = sp.plan(s, "h2", None, MEDIA, None)
    assert silent.timeline["voice"] is None and silent.timeline["frames"] == 105
    spoken = sp.plan(s, "h1", take(H1[:5]), MEDIA, "x.mp3")
    assert spoken.timeline["voice"] and sum(v["frames"] for v in spoken.timeline["video"]) == spoken.timeline["frames"]
