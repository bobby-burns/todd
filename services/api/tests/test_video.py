"""The video toolset: storyboard, story-state ranking, the asset cache, pick and render, and the Files view's video
route. The real sandbox server on a temp folder, a fake media service (vectors chosen by the test, placeholder slides
and MP4) and a fake Pexels: no FFmpeg, no models, no network."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from todd import media_index, registry, vault
from todd.config import config
from todd.db import MediaAsset, session
from todd.integrations import assess, find_integrations
from todd.runtime import RunContext
from todd.sdk import ToolError, _current_ctx, set_ctx
from todd.tools import media, sandbox, video

from .helpers import events_of, new_run

SERVER = Path(__file__).resolve().parents[2] / "sandbox" / "server.py"
MODEL = "fake-clip-vision"
DIM = 512


def vec(**weights: float) -> list[float]:
    """A unit vector from named axes, e.g. vec(hockey=0.9, warm=0.4)."""
    v = [0.0] * DIM
    for name, w in weights.items():
        v[AXES[name]] += w
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


AXES = {n: i for i, n in enumerate(["hockey", "rink", "skates", "warm", "cold", "phone", "other"])}


def noise(key: str) -> list[float]:
    """A unit vector for images and texts the test didn't set: far from the named axes and from each other."""
    rnd = random.Random(key)
    v = [0.0] * len(AXES) + [rnd.gauss(0, 1) for _ in range(DIM - len(AXES))]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


class FakeMedia:
    """Stands in for the media container: vectors come from the test's tables, files land in the run folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.text: dict[str, list[float]] = {}  # substring of the query -> vector
        self.urls: dict[str, list[float]] = {}  # substring of the URL -> vector
        self.paths: dict[str, list[float]] = {}  # workspace path -> vector
        self.calls: list[tuple[str, dict]] = []
        self.render_error: str | None = None  # make /render/slideshow fail with this
        self.dead_urls: set[str] = set()  # /fetch of these fails (an expired download link)

    def _file(self, run_id: str, save_dir: str, url: str) -> tuple[str, str]:
        data = f"image:{url.split('?')[0]}".encode()
        sha = hashlib.sha256(data).hexdigest()
        p = self.root / run_id / save_dir / f"{sha}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"{save_dir}/{sha}.jpg", sha

    async def call(self, path: str, payload: dict, timeout: float = 120) -> dict:
        self.calls.append((path, payload))
        if path == "/embed/text":
            out = []
            for t in payload["texts"]:
                out.append(next((v for key, v in self.text.items() if key in t), noise(t)))
            return {"model": "fake-clip-text", "image_model": MODEL, "dim": DIM, "vectors": out}
        if path == "/embed/images":
            results = []
            for item in payload["items"]:
                if item.get("url"):
                    rel, sha = self._file(payload["run_id"], payload["save_dir"], item["url"])
                    v = next((v for key, v in self.urls.items() if key in item["url"]), noise(item["url"]))
                    results.append({"ok": True, "vector": v, "sha256": sha, "width": 1280, "height": 1920,
                                    "path": rel, "unchanged": False, "error": None})
                    continue
                f = self.root / payload["run_id"] / item["path"]
                if not f.is_file():
                    results.append({"ok": False, "error": f"no such file: {item['path']}"})
                    continue
                sha = hashlib.sha256(f.read_bytes()).hexdigest()
                same = sha == item.get("known_sha256")
                v = self.paths.get(item["path"]) or noise(f"{item['path']}:{sha}")
                results.append({"ok": True, "vector": None if same else v,
                                "sha256": sha, "width": 1170, "height": 2532, "path": item["path"], "unchanged": same,
                                "error": None})
            return {"model": MODEL, "dim": DIM, "results": results}
        if path == "/fetch":
            if payload["url"] in self.dead_urls:
                raise ToolError("media /fetch -> 502: fetch failed: HTTP 410")
            rel, sha = self._file(payload["run_id"], payload["save_dir"], payload["url"])
            return {"path": rel, "sha256": sha}
        if path == "/slides/compose":
            out_dir = self.root / payload["run_id"] / payload["out_dir"]
            out_dir.mkdir(parents=True, exist_ok=True)
            slides = []
            for i, s in enumerate(payload["slides"], 1):
                assert (self.root / payload["run_id"] / s["image"]).is_file()
                (out_dir / f"{i:02d}.png").write_bytes(b"\x89PNG placeholder")
                slides.append(f"{payload['out_dir']}/{i:02d}.png")
            (self.root / payload["run_id"] / payload["sheet"]).write_bytes(b"\x89PNG sheet")
            return {"slides": slides, "sheet": payload["sheet"]}
        if path == "/render/slideshow":
            if self.render_error:
                raise ToolError(f"media /render/slideshow -> 500: {self.render_error}")
            for s in payload["slides"]:
                assert (self.root / payload["run_id"] / s).is_file()
            out = self.root / payload["run_id"] / payload["out"]
            out.write_bytes(b"\x00\x00\x00\x20ftypisom placeholder mp4")
            return {"path": payload["out"], "duration_s": round(sum(payload["durations_s"]), 3),
                    "size_bytes": out.stat().st_size}
        raise AssertionError(f"unexpected media call {path}")

    def count(self, path: str, kind: str | None = None) -> int:
        return sum(1 for p, body in self.calls if p == path
                   and (kind is None or any(kind in item for item in body.get("items", []))))


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A run with the sandbox's real file server on tmp_path, a fake media service and a fake Pexels."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    spec = importlib.util.spec_from_file_location("todd_sandbox_server_video", SERVER)
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

    photos: dict[str, list[dict]] = {}  # query substring -> Pexels photos
    searches: list[str] = []

    async def pexels_search(key, query, per_page=15):
        assert key == "pexels-test-key"
        searches.append(query)
        return next((p for q, p in photos.items() if q in query), [])

    monkeypatch.setattr(video, "pexels_search", pexels_search)
    hits: dict[str, list[dict]] = {}  # query substring -> Pixabay hits

    async def pixabay_search(key, query, per_page=20):
        assert key == "pixabay-test-key"
        searches.append(f"pixabay:{query}")
        return next((h for q, h in hits.items() if q in query), [])

    monkeypatch.setattr(video, "pixabay_search", pixabay_search)
    vault.set_secret("PEXELS_API_KEY", "pexels-test-key")  # Pixabay's key only where a test sets it
    rid = new_run("video")
    (tmp_path / rid).mkdir()
    ctx = RunContext(rid)
    tok = set_ctx(ctx)
    yield {"run": rid, "dir": tmp_path / rid, "media": fake, "photos": photos, "hits": hits, "searches": searches,
           "ctx": ctx}
    _current_ctx.reset(tok)
    vault.delete_secret("PEXELS_API_KEY")
    vault.delete_secret("PIXABAY_API_KEY")


def photo(pid: str, photographer: str = "Ana", pg_id: int = 1, width: int = 3000) -> dict:
    return {"id": pid, "width": width, "height": 4500, "photographer": photographer, "photographer_id": pg_id,
            "photographer_url": f"https://www.pexels.com/@{photographer.lower()}",
            "alt": f"photo {pid}", "src": {"original": f"https://images.pexels.com/photos/{pid}/pexels-photo-{pid}.jpeg",
                                          "large2x": f"https://images.pexels.com/photos/{pid}/x.jpeg?h=650"}}


def hit(hid: str, user: str = "Kai", user_id: int = 5, full_hd: bool = False) -> dict:
    """A Pixabay search hit."""
    out = {"id": int(hid), "pageURL": f"https://pixabay.com/photos/ice-hockey-{hid}/", "type": "photo",
           "tags": f"hockey, rink, {hid}", "largeImageURL": f"https://pixabay.com/get/g{hid}_1280.jpg",
           "user_id": user_id, "user": user}
    if full_hd:
        out["fullHDURL"] = f"https://pixabay.com/get/g{hid}_1920.jpg"
    return out


def uid() -> str:
    return str(uuid.uuid4().int)[:9]  # Pexels-like ids, new per test: the asset cache is shared by the session


SHOTS = [{"text": "hockey players on a rink", "caption": "Pickup hockey, tonight"},
         {"text": "skates being laced", "caption": "No group chat needed", "caption_position": "top"},
         {"text": "phone showing the app", "caption": "dropin.hockey", "duration_s": 3}]


async def new_board(**kw) -> dict:
    return await video.video_new.ainvoke({"title": "Dropin Hockey launch", "shots": SHOTS, **kw})


def board(studio, slug: str) -> dict:
    return json.loads((studio["dir"] / "video" / slug / "storyboard.json").read_text())


def ids(result: dict) -> list[str]:
    return [c["asset_id"] for c in result["candidates"]]


def test_video_new_validates_and_writes_the_storyboard(loop, studio):
    async def go():
        bad = [({"shots": SHOTS[:2]}, "3–12 shots"),
               ({"shots": SHOTS * 5}, "3–12 shots"),
               ({"shots": [*SHOTS[:2], {"text": "x", "caption": "y", "duration_s": 9}]}, "1.5–6"),
               ({"shots": [*SHOTS[:2], {"text": "x", "caption": "y", "duration_s": 1}]}, "1.5–6"),
               ({"shots": [*SHOTS[:2], {"text": "x", "caption": "c" * 91}]}, "91 characters"),
               ({"shots": [*SHOTS[:2], {"caption": "no text"}]}, "needs a \"text\""),
               ({"shots": [*SHOTS[:2], {"text": "x", "caption_position": "left"}]}, "caption_position"),
               ({"shots": SHOTS, "aspect": "16:9"}, "9:16")]
        for kw, msg in bad:
            with pytest.raises(ToolError, match=msg):
                await video.video_new.ainvoke({"title": "t", **kw})
        r = await new_board(style_note="bright,  natural light")
        assert r["slug"] == "dropin-hockey-launch" and r["path"] == "video/dropin-hockey-launch/storyboard.json"
        b = board(studio, r["slug"])
        assert (b["version"], b["aspect"], b["width"], b["height"]) == (1, "9:16", 1080, 1920)
        assert b["style_note"] == "bright, natural light"
        assert [s["id"] for s in b["shots"]] == ["s1", "s2", "s3"]
        s1, s2, s3 = b["shots"]
        assert (s1["caption_position"], s1["duration_s"], s1["status"], s1["pick"]) == ("middle", 2.5, "open", None)
        assert s2["caption_position"] == "top" and s3["duration_s"] == 3.0
        again = await new_board()
        assert again["slug"] == "dropin-hockey-launch-2"  # unique within the run
    loop.run_until_complete(go())


def test_text_match_wins_before_anything_is_picked(loop, studio):
    a, b, c = uid(), uid(), uid()
    studio["photos"]["hockey players"] = [photo(a), photo(b), photo(c, width=900)]  # c is too narrow
    m = studio["media"]
    m.text["hockey players"] = vec(hockey=1)
    m.urls[a], m.urls[b] = vec(hockey=0.6, warm=0.8), vec(hockey=0.9, cold=0.4359)

    async def go():
        slug = (await new_board(style_note="natural light"))["slug"]
        r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1"})
        by_id = {x["asset_id"]: x for x in r["candidates"]}
        assert [by_id[i]["alt"] for i in ids(r)] == [f"photo {b}", f"photo {a}"]
        top = r["candidates"][0]
        assert top["style_score"] == 0 and top["score"] == top["text_score"] == pytest.approx(0.9, abs=1e-3)
        assert top["source"] == "pexels" and top["creator"] == "Ana" and top["preview"].endswith("&h=1920")
        assert r["query"] == "hockey players on a rink, natural light" and r["state"] == "0 of 3 shots picked"
        assert studio["searches"] == ["hockey players on a rink"]  # Pexels gets the words, not the style note
        assert board(studio, slug)["shots"][0]["candidates"] == ids(r)
        ev = [e for e in events_of(studio["run"]) if e.kind == "step" and e.text == "Found 2 options for s1"]
        assert len(ev) == 1 and ev[0].data["candidates"] == ids(r)
    loop.run_until_complete(go())


def test_style_of_picked_shots_changes_the_ranking(loop, studio):
    first, warm, cold = uid(), uid(), uid()
    studio["photos"]["hockey players"] = [photo(first)]
    studio["photos"]["skates"] = [photo(warm, "Bo", 2), photo(cold, "Cy", 3)]
    m = studio["media"]
    m.text["hockey players"], m.text["skates"] = vec(hockey=1), vec(skates=1)
    m.urls[first] = vec(hockey=0.8, warm=0.6)
    m.urls[warm] = vec(skates=0.7, warm=0.71)  # a little less on-topic, same look as the first pick
    m.urls[cold] = vec(skates=0.75, cold=0.66)

    async def go():
        slug = (await new_board())["slug"]
        before = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s2"})
        assert [c["alt"] for c in before["candidates"]] == [f"photo {cold}", f"photo {warm}"]  # text alone

        r1 = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1"})
        await video.video_pick.ainvoke({"slug": slug, "shot_id": "s1", "asset_id": ids(r1)[0]})
        after = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s2"})
        assert [c["alt"] for c in after["candidates"]] == [f"photo {warm}", f"photo {cold}"]
        w, c = after["candidates"]
        assert w["style_score"] > 0.4 > c["style_score"] and w["text_score"] < c["text_score"]
        assert w["score"] == pytest.approx(w["text_score"] + video.STYLE_WEIGHT * w["style_score"], abs=1e-3)
        assert after["state"] == "1 of 3 shots picked"

        # searching the picked shot again: its own pick is an option, not part of the story state
        again = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1"})
        assert ids(again) == ids(r1) and again["candidates"][0]["style_score"] == 0
    loop.run_until_complete(go())


def test_picks_and_near_duplicates_are_left_out_and_same_creator_gets_a_bonus(loop, studio):
    p1, dup, same, other = uid(), uid(), uid(), uid()
    studio["photos"]["hockey players"] = [photo(p1, "Ana", 7)]
    studio["photos"]["skates"] = [photo(p1, "Ana", 7), photo(dup, "Dee", 8), photo(same, "Ana", 7),
                                  photo(other, "Eve", 9)]
    m = studio["media"]
    m.text["hockey players"], m.text["skates"] = vec(hockey=1), vec(skates=1)
    m.urls[p1] = vec(hockey=0.7, skates=0.7)
    m.urls[dup] = vec(hockey=0.69, skates=0.72)  # cos ≈ 0.9998 with p1: the same picture
    m.urls[same] = vec(skates=0.6, phone=0.8)
    m.urls[other] = vec(skates=0.6, rink=0.8)  # same text fit and style fit as `same`

    async def go():
        slug = (await new_board())["slug"]
        r1 = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1"})
        await video.video_pick.ainvoke({"slug": slug, "shot_id": "s1", "asset_id": ids(r1)[0]})
        r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s2", "k": 10})
        alts = [c["alt"] for c in r["candidates"]]
        assert f"photo {p1}" not in alts and f"photo {dup}" not in alts
        assert alts == [f"photo {same}", f"photo {other}"]
        s, o = r["candidates"]
        assert s["same_creator"] and "same_creator" not in o
        assert s["score"] - o["score"] == pytest.approx(video.CREATOR_BONUS, abs=1e-3)
    loop.run_until_complete(go())


def test_stock_photos_are_embedded_once(loop, studio):
    a, b = uid(), uid()
    studio["photos"]["hockey players"] = [photo(a), photo(b)]
    m = studio["media"]

    async def go():
        slug = (await new_board())["slug"]
        await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1", "sources": ["pexels"]})
        assert m.count("/embed/images", "url") == 1
        again = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1", "sources": ["pexels"]})
        assert m.count("/embed/images", "url") == 1 and len(again["candidates"]) == 2
        with session() as s:
            rows = s.exec(media_index.select(MediaAsset).where(MediaAsset.source == "pexels",
                                                               MediaAsset.source_id.in_([a, b]))).all()
        assert len(rows) == 2 and {r.license for r in rows} == {"pexels"} and {r.model for r in rows} == {MODEL}

        # another run finds the same photos: still not embedded again; the file is fetched into that run's folder
        other = new_run("video 2")
        (studio["dir"].parent / other).mkdir()
        tok = set_ctx(RunContext(other))
        try:
            slug2 = (await new_board())["slug"]
            r = await video.video_find_shots.ainvoke({"slug": slug2, "shot_id": "s1", "sources": ["pexels"]})
            assert m.count("/embed/images", "url") == 1
            p = await video.video_pick.ainvoke({"slug": slug2, "shot_id": "s1", "asset_id": ids(r)[0]})
            assert (studio["dir"].parent / other / p["path"]).is_file() and m.count("/fetch") == 1
        finally:
            _current_ctx.reset(tok)
    loop.run_until_complete(go())


def test_missing_pexels_key_and_the_runs_own_images(loop, studio):
    d = studio["dir"]
    (d / "video" / "library").mkdir(parents=True)
    (d / "video" / "library" / "home.png").write_bytes(b"home screen")
    (d / "video" / "library" / "notes.txt").write_text("not an image")
    (d / "app").mkdir()
    (d / "app" / "map.png").write_bytes(b"map screen")
    m = studio["media"]
    m.text["phone"] = vec(phone=1)
    m.paths["video/library/home.png"] = vec(phone=0.9, warm=0.44)
    m.paths["app/map.png"] = vec(phone=0.6, rink=0.8)
    vault.delete_secret("PEXELS_API_KEY")

    async def go():
        slug = (await new_board())["slug"]
        with pytest.raises(ToolError) as e:  # no stock key at all: ask for Pixabay's (Pexels has paused new keys)
            await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s3"})
        assert "https://pixabay.com/api/docs/" in str(e.value) and 'secret_name="PIXABAY_API_KEY"' in str(e.value)
        assert "paused new keys" in str(e.value) and 'sources=["workspace"]' in str(e.value)
        for source, msg in (("pexels", 'paused new keys. Use sources=["pixabay"]'),
                            ("pixabay", 'secret_name="PIXABAY_API_KEY"')):
            with pytest.raises(ToolError, match=re.escape(msg)):
                await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s3", "sources": [source]})

        r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s3", "sources": ["workspace"],
                                                  "workspace_paths": ["app/map.png"]})
        assert [c["preview"] for c in r["candidates"]] == ["video/library/home.png", "app/map.png"]
        assert {c["source"] for c in r["candidates"]} == {"workspace"} and studio["searches"] == []
        embedded = m.count("/embed/images")

        # unchanged files aren't embedded again (they're hashed, not re-embedded); a changed one is
        r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s3", "sources": ["workspace"]})
        last = m.calls[-1][1]
        assert m.count("/embed/images") == embedded + 1 and all(i["known_sha256"] for i in last["items"])
        assert "app/map.png" in [c["preview"] for c in r["candidates"]]  # still in the run's library
        m.paths["video/library/home.png"] = vec(cold=1)
        (d / "video" / "library" / "home.png").write_bytes(b"new home screen")
        r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s3", "sources": ["workspace"]})
        assert [c["preview"] for c in r["candidates"]] == ["app/map.png", "video/library/home.png"]

        # a file that left the library isn't offered any more
        (d / "video" / "library" / "home.png").unlink()
        r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s3", "sources": ["workspace"]})
        assert [c["preview"] for c in r["candidates"]] == ["app/map.png"]

        p = await video.video_pick.ainvoke({"slug": slug, "shot_id": "s3", "asset_id": ids(r)[0]})
        assert p["path"] == f"video/{slug}/assets/s3.png"
        assert (d / p["path"]).read_bytes() == b"map screen"
        assert p["open_shots"] == ["s1", "s2"] and p["state"] == "1 of 3 shots picked"
    loop.run_until_complete(go())


def test_workspace_paths_outside_the_run_folder_are_refused(loop, studio):
    async def go():
        slug = (await new_board())["slug"]
        for bad in ("../other-run/x.png", "/etc/passwd.png", "app/../../x.png", f"{studio['dir'].parent}/x.png"):
            with pytest.raises(ToolError, match="outside this run's folder"):
                await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1", "sources": ["workspace"],
                                                      "workspace_paths": [bad]})
        with pytest.raises(ToolError, match="images"):
            await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1", "sources": ["workspace"],
                                                  "workspace_paths": ["app/.env"]})
        with pytest.raises(ToolError, match="bad slug"):
            await video.video_find_shots.ainvoke({"slug": "../x", "shot_id": "s1"})
        with pytest.raises(ToolError, match="no storyboard"):
            await video.video_find_shots.ainvoke({"slug": "nope", "shot_id": "s1"})
        with pytest.raises(ToolError, match="bad shot_id"):
            await video.video_pick.ainvoke({"slug": slug, "shot_id": "../../x", "asset_id": "a"})
        with pytest.raises(ToolError, match="no shot 's9'"):
            await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s9"})
        # an absolute path inside the run folder is fine
        (studio["dir"] / "shot.png").write_bytes(b"x")
        r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1", "sources": ["workspace"],
                                                  "workspace_paths": [f"{studio['dir']}/shot.png"]})
        assert [c["preview"] for c in r["candidates"]] == ["shot.png"]
    loop.run_until_complete(go())


def test_pick_only_takes_a_shots_candidates(loop, studio):
    a, b = uid(), uid()
    studio["photos"]["hockey players"] = [photo(a)]
    studio["photos"]["skates"] = [photo(b)]

    async def go():
        slug = (await new_board())["slug"]
        r1 = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1"})
        r2 = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s2"})
        with pytest.raises(ToolError, match="isn't one of s1's candidates"):
            await video.video_pick.ainvoke({"slug": slug, "shot_id": "s1", "asset_id": ids(r2)[0]})
        with pytest.raises(ToolError, match="isn't one of s1's candidates"):
            await video.video_pick.ainvoke({"slug": slug, "shot_id": "s1", "asset_id": "made-up"})
        p = await video.video_pick.ainvoke({"slug": slug, "shot_id": "s1", "asset_id": ids(r1)[0]})
        assert p["path"] == f"video/{slug}/assets/s1.jpg" and (studio["dir"] / p["path"]).is_file()
        s1 = board(studio, slug)["shots"][0]
        assert s1["status"] == "picked" and s1["pick"]["asset_id"] == ids(r1)[0]
        assert s1["pick"]["score"] == r1["candidates"][0]["score"]
    loop.run_until_complete(go())


def test_render_needs_every_shot_and_credits_the_photographers(loop, studio):
    a, b = uid(), uid()
    studio["photos"]["hockey players"] = [photo(a, "Ana Lee", 11)]
    studio["photos"]["skates"] = [photo(b, "Bo Kim", 12)]
    (studio["dir"] / "video" / "library").mkdir(parents=True)
    (studio["dir"] / "video" / "library" / "app.png").write_bytes(b"app")
    m = studio["media"]

    async def go():
        slug = (await new_board())["slug"]
        picks = {}
        for sid, sources in (("s1", ["pexels"]), ("s2", ["pexels"]), ("s3", ["workspace"])):
            r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": sid, "sources": sources})
            picks[sid] = ids(r)[0]
            if sid == "s2":
                with pytest.raises(ToolError, match="s3 still open"):
                    await video.video_render.ainvoke({"slug": slug})
                assert m.count("/slides/compose") == 0 and m.count("/render/slideshow") == 0
            await video.video_pick.ainvoke({"slug": slug, "shot_id": sid, "asset_id": picks[sid]})
        r = await video.video_render.ainvoke({"slug": slug})
        assert r["video"] == f"video/{slug}/{slug}.mp4" and (studio["dir"] / r["video"]).is_file()
        assert r["duration_s"] == 8.0  # 2.5 + 2.5 + 3
        assert r["slides"] == [f"video/{slug}/slides/0{i}.png" for i in (1, 2, 3)]
        assert r["preview"] == f"video/{slug}/preview.png" and r["credits"] == f"video/{slug}/CREDITS.md"
        req = next(body for p, body in m.calls if p == "/slides/compose")
        assert (req["width"], req["height"]) == (1080, 1920)
        assert [(s["image"], s["caption"], s["caption_position"], s["fit"]) for s in req["slides"]] == [
            (f"video/{slug}/assets/s1.jpg", "Pickup hockey, tonight", "middle", "cover"),
            (f"video/{slug}/assets/s2.jpg", "No group chat needed", "top", "cover"),
            (f"video/{slug}/assets/s3.png", "dropin.hockey", "middle", "contain")]  # the run's own image: whole
        req = next(body for p, body in m.calls if p == "/render/slideshow")
        assert req["slides"] == r["slides"] and req["durations_s"] == [2.5, 2.5, 3.0]
        assert (req["out"], req["fps"], req["motion"], req["music"]) == (r["video"], 30, "kenburns", None)
        credits = (studio["dir"] / r["credits"]).read_text()
        assert "[Ana Lee](https://www.pexels.com/@ana lee)" in credits and "Bo Kim" in credits
        assert f"https://www.pexels.com/photo/{a}/" in credits and f"https://www.pexels.com/photo/{b}/" in credits
        assert "s3: video/library/app.png" in credits
        ev = [e for e in events_of(studio["run"]) if e.text == f"Rendered {slug}.mp4: 3 slides, 8s"]
        assert len(ev) == 1 and ev[0].data["video"] == r["video"]

        # music from the run folder, still slides; bad options are refused before anything is rendered
        n = m.count("/render/slideshow")
        for kw, msg in (({"motion": "zoom"}, "kenburns or none"),
                        ({"music_path": "../other-run/song.mp3"}, "outside this run's folder"),
                        ({"music_path": "notes.txt"}, "audio files")):
            with pytest.raises(ToolError, match=msg):
                await video.video_render.ainvoke({"slug": slug, **kw})
        assert m.count("/render/slideshow") == n
        r = await video.video_render.ainvoke({"slug": slug, "music_path": f"{studio['dir']}/audio/song.mp3",
                                              "motion": "none"})
        req = [body for p, body in m.calls if p == "/render/slideshow"][-1]
        assert (req["music"], req["motion"]) == ("audio/song.mp3", "none")

        # FFmpeg fails: the agent hears the slides are still there
        m.render_error = "ffmpeg failed: something"
        with pytest.raises(ToolError, match=f"slides are ready in video/{slug}/slides/, but the MP4 failed"):
            await video.video_render.ainvoke({"slug": slug})
    loop.run_until_complete(go())


def test_nearest_on_sqlite():
    tag = uid()
    rows = [MediaAsset(source="pexels", source_id=f"{tag}-{i}", sha256=f"{tag}{i}", model=f"m-{tag}",
                       run_id=f"r{tag}" if i < 2 else None, embedding=v)
            for i, v in enumerate([vec(hockey=1), vec(hockey=0.8, warm=0.6), vec(hockey=0.3, warm=1), vec(cold=1)])]
    with session() as s:
        s.add_all(rows)
        s.commit()
    got = media_index.nearest(vec(hockey=1), 3, source="pexels", model=f"m-{tag}")
    assert [a.source_id for a, _ in got] == [f"{tag}-0", f"{tag}-1", f"{tag}-2"]
    assert got[0][1] == pytest.approx(1.0) and got[1][1] == pytest.approx(0.8)
    got = media_index.nearest(vec(hockey=1), 5, model=f"m-{tag}", exclude_ids=[rows[0].id])
    assert [a.source_id for a, _ in got] == [f"{tag}-1", f"{tag}-2", f"{tag}-3"]
    assert [a.source_id for a, _ in media_index.nearest(vec(warm=1), 5, model=f"m-{tag}", run_id=f"r{tag}")] == \
        [f"{tag}-1", f"{tag}-0"]
    assert media_index.nearest(vec(hockey=1), 5, model="no-such-model") == []
    assert not media_index.active()  # SQLite: the Python fallback


def test_video_toolset_and_pexels_routing(loop):
    ts = registry.all_toolsets()
    assert [t.name for t in ts["video"].tools] == ["video_new", "video_find_shots", "video_pick", "video_render"]
    assert ts["video"].source == "builtin" and "video_find_shots" in ts["video"].guide
    assert "video" in registry.toolsets_prompt(ts)
    vault.set_secret("PEXELS_API_KEY", "pexels-test-key")
    vault.set_secret("PIXABAY_API_KEY", "pixabay-test-key")
    try:
        ctx = RunContext(new_run("stock photos"))
        ctx.toolsets = {"video": ts["video"]}  # type: ignore[attr-defined]
        tok = set_ctx(ctx)
        try:
            r = loop.run_until_complete(find_integrations.ainvoke({"services": ["pixabay", "pexels", "stock photos"]}))
        finally:
            _current_ctx.reset(tok)
        assert [s["route"] for s in r["services"]] == ["toolset", "toolset", "toolset"]
        assert [s["service"] for s in r["services"]] == ["Pixabay", "Pexels", "Pixabay"]
        assert "`video` toolset" in r["services"][0]["recommendation"]
    finally:
        vault.delete_secret("PEXELS_API_KEY")
        vault.delete_secret("PIXABAY_API_KEY")
    no_key = assess("pixabay", {"video"})
    assert no_key["route"] != "toolset" and "https://pixabay.com/api/docs/" in no_key["recommendation"]
    assert "paused new API keys" in assess("pexels", {"video"})["recommendation"]


def test_files_media_route_plays_video_with_ranges(studio, monkeypatch):
    from todd import main

    rid, d = studio["run"], studio["dir"]
    data = bytes(range(256)) * 40  # 10,240 bytes standing in for an MP4
    (d / "video" / "show").mkdir(parents=True)
    (d / "video" / "show" / "show.mp4").write_bytes(data)
    (d / "video" / "show" / "slide.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(config, "api_token", "")
    c = TestClient(main.app)
    url = f"/api/runs/{rid}/files/media"

    r = c.get(url, params={"path": "video/show/show.mp4"})
    assert r.status_code == 200 and r.content == data and r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes" and r.headers["content-disposition"].startswith("inline")
    for rng, start, end in (("bytes=0-1", 0, 1), ("bytes=100-", 100, 10239), ("bytes=10000-99999", 10000, 10239),
                            ("bytes=-40", 10200, 10239)):
        r = c.get(url, params={"path": "video/show/show.mp4"}, headers={"Range": rng})
        assert r.status_code == 206, rng
        assert r.content == data[start:end + 1] and r.headers["content-range"] == f"bytes {start}-{end}/10240"
        assert int(r.headers["content-length"]) == end - start + 1
    r = c.get(url, params={"path": "video/show/show.mp4"}, headers={"Range": "bytes=20000-"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */10240"

    assert c.get(url, params={"path": "video/show/slide.png"}).status_code == 400  # not a video
    assert c.get(url, params={"path": "video/show/storyboard.json"}).status_code == 400
    assert c.get(url, params={"path": "video/show/missing.mp4"}).status_code == 404
    assert c.get(url, params={"path": "../other/show.mp4"}).status_code in (400, 404)
    assert c.get("/api/runs/nope/files/media", params={"path": "a.mp4"}).status_code == 404


def test_pixabay_photos_are_searched_picked_and_credited(loop, studio, monkeypatch):
    a, b, c = uid(), uid(), uid()
    studio["hits"]["hockey players"] = [hit(a, "Kai", 5), hit(b, "Lu", 6, full_hd=True)]
    studio["hits"]["skates"] = [hit(c, "Kai", 5)]
    studio["photos"]["hockey players"] = [photo(uid(), "Ana", 1)]
    (studio["dir"] / "video" / "library").mkdir(parents=True)
    (studio["dir"] / "video" / "library" / "app.png").write_bytes(b"app")
    m = studio["media"]
    m.text["hockey players"], m.text["skates"] = vec(hockey=1), vec(skates=1)
    m.urls[f"g{a}_"], m.urls[f"g{b}_"], m.urls[f"g{c}_"] = vec(hockey=0.95, warm=0.3), vec(hockey=0.5, cold=0.86), \
        vec(skates=0.8, warm=0.6)
    vault.set_secret("PIXABAY_API_KEY", "pixabay-test-key")

    async def go():
        slug = (await new_board())["slug"]
        r = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s1"})  # default: every source with a key
        assert {"pixabay:hockey players on a rink", "hockey players on a rink"} <= set(studio["searches"])
        assert {x["source"] for x in r["candidates"]} == {"pixabay", "pexels", "workspace"}
        top = r["candidates"][0]
        assert (top["source"], top["creator"], top["alt"]) == ("pixabay", "Kai", f"hockey, rink, {a}")
        urls = [i["url"] for _, body in m.calls if _ == "/embed/images" for i in body["items"] if "url" in i]
        assert f"https://pixabay.com/get/g{a}_1280.jpg" in urls
        assert f"https://pixabay.com/get/g{b}_1920.jpg" in urls  # full API access: the 1920 px image
        with session() as s:
            row = s.exec(media_index.select(MediaAsset).where(MediaAsset.source == "pixabay",
                                                              MediaAsset.source_id == a)).one()
        assert (row.license, row.creator_id, row.creator_url) == ("pixabay", "5", "https://pixabay.com/users/Kai-5/")
        assert row.page_url == f"https://pixabay.com/photos/ice-hockey-{a}/"

        await video.video_pick.ainvoke({"slug": slug, "shot_id": "s1", "asset_id": top["asset_id"]})
        r2 = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s2", "sources": ["pixabay"]})
        assert r2["candidates"][0]["same_creator"]  # same Pixabay user as the s1 pick

        # its download link expired: video_pick asks Pixabay for a fresh one and keeps going
        fresh = {**hit(c, "Kai", 5), "largeImageURL": f"https://pixabay.com/get/fresh{c}_1280.jpg"}
        looked_up = []

        async def pixabay_image(key, image_id):
            looked_up.append(image_id)
            return fresh

        monkeypatch.setattr(video, "pixabay_image", pixabay_image)
        m.dead_urls.add(f"https://pixabay.com/get/g{c}_1280.jpg")
        p = await video.video_pick.ainvoke({"slug": slug, "shot_id": "s2", "asset_id": ids(r2)[0]})
        assert looked_up == [c] and (studio["dir"] / p["path"]).is_file()
        with session() as s:
            assert s.get(MediaAsset, ids(r2)[0]).url == f"https://pixabay.com/get/fresh{c}_1280.jpg"

        r3 = await video.video_find_shots.ainvoke({"slug": slug, "shot_id": "s3", "sources": ["workspace"]})
        await video.video_pick.ainvoke({"slug": slug, "shot_id": "s3", "asset_id": ids(r3)[0]})
        out = await video.video_render.ainvoke({"slug": slug})
        req = next(body for p, body in m.calls if p == "/slides/compose")
        assert [s["fit"] for s in req["slides"]] == ["cover", "cover", "contain"]  # stock fills, screenshots whole
        credits = (studio["dir"] / out["credits"]).read_text()
        assert "Images from [Pixabay](https://pixabay.com)" in credits and "Pexels" not in credits
        assert f"- s1: photo by [Kai](https://pixabay.com/users/Kai-5/) on Pixabay: " \
               f"https://pixabay.com/photos/ice-hockey-{a}/" in credits
    loop.run_until_complete(go())


def test_pixabay_search_asks_for_portrait_photos_and_caches_for_a_day(loop, monkeypatch):
    calls: list[dict] = []
    now = [1_000_000.0]

    async def get(params):
        calls.append(params)
        return [hit("1")]

    monkeypatch.setattr(video, "_pixabay_get", get)
    monkeypatch.setattr(video.time, "time", lambda: now[0])
    monkeypatch.setattr(video, "_pixabay_cache", {})
    long = "hockey " * 30

    async def go():
        assert (await video.pixabay_search("k", long))[0]["id"] == 1
        await video.pixabay_search("k", long)
        assert len(calls) == 1  # Pixabay asks for responses to be cached for 24 hours
        q = calls[0]
        assert len(q["q"]) == 100 and (q["orientation"], q["image_type"], q["safesearch"]) == \
            ("vertical", "photo", "true")
        assert q["min_height"] == video.PIXABAY_MIN_HEIGHT and q["key"] == "k"
        now[0] += video.PIXABAY_CACHE_S + 1
        await video.pixabay_search("k", long)
        assert len(calls) == 2
    loop.run_until_complete(go())
