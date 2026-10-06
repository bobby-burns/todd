"""The media server on a temp folder with fake embeddings (MEDIA_FAKE_EMBED=1): auth, confinement to the run's folder,
the fetch allowlist, embeddings, slide composition and MP4 rendering. Needs Pillow, numpy, the DejaVu fonts and FFmpeg;
no models, no network."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

SERVER = Path(__file__).resolve().parents[1] / "server.py"
RUN = "run1"
H = {"X-Media-Token": "t"}


def load(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("MEDIA_FAKE_EMBED", "1")
    monkeypatch.delenv("MEDIA_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MEDIA_TOKEN", "t")
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    (tmp_path / "ws" / RUN).mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("todd_media_server_test", SERVER)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)  # pydantic resolves the request models' annotations through it
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def media(tmp_path, monkeypatch):
    mod = load(monkeypatch, tmp_path)
    return mod, TestClient(mod.app), tmp_path / "ws" / RUN


def png(color=(200, 30, 30), size=(800, 1200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def jpeg(color=(30, 30, 200), size=(1200, 1800)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def test_token_required(tmp_path, monkeypatch):
    mod = load(monkeypatch, tmp_path)
    c = TestClient(mod.app)
    assert c.post("/embed/text", json={"texts": ["x"]}).status_code == 401
    assert c.post("/embed/text", json={"texts": ["x"]}, headers={"X-Media-Token": "nope"}).status_code == 401
    assert c.post("/embed/text", json={"texts": ["x"]}, headers=H).status_code == 200
    assert c.get("/health").json()["dim"] == 512

    # no token configured at all: everything is refused
    mod = load(monkeypatch, tmp_path, MEDIA_TOKEN=None)
    c = TestClient(mod.app)
    assert c.post("/embed/text", json={"texts": ["x"]}, headers={"X-Media-Token": ""}).status_code == 401

    # the token file the API makes; missing file -> refused
    tf = tmp_path / "token"
    mod = load(monkeypatch, tmp_path, MEDIA_TOKEN=None, MEDIA_TOKEN_FILE=str(tf))
    c = TestClient(mod.app)
    assert c.post("/embed/text", json={"texts": ["x"]}, headers=H).status_code == 401
    tf.write_text("t\n")
    assert c.post("/embed/text", json={"texts": ["x"]}, headers=H).status_code == 200


def test_paths_stay_in_the_run_folder(media, tmp_path):
    _, c, run = media
    (tmp_path / "ws" / "other").mkdir()
    (tmp_path / "ws" / "other" / "x.png").write_bytes(png())
    (tmp_path / "outside.png").write_bytes(png())
    os.symlink(tmp_path / "outside.png", run / "peek.png")
    os.symlink(tmp_path / "ws" / "other", run / "peekdir")

    r = c.post("/embed/images", json={"run_id": RUN, "items": [
        {"path": "../other/x.png"}, {"path": "/../../outside.png"}, {"path": "peek.png"}, {"path": "peekdir/x.png"}]},
        headers=H).json()
    assert [x["ok"] for x in r["results"]] == [False] * 4
    assert all("outside" in x["error"] for x in r["results"])
    r = c.post("/slides/compose", json={"run_id": RUN, "slides": [{"image": "peek.png"}], "out_dir": "slides"},
               headers=H)
    assert r.status_code == 400
    r = c.post("/slides/compose", json={"run_id": RUN, "slides": [{"image": "a.png"}], "out_dir": "../escape"},
               headers=H)
    assert r.status_code == 400
    for bad in ("..", "../ws", "a/b", ""):
        assert c.post("/fetch", json={"run_id": bad, "url": "https://images.pexels.com/x.jpg"},
                      headers=H).status_code in (400, 422)
    assert c.post("/fetch", json={"run_id": RUN, "url": "https://images.pexels.com/x.jpg", "save_dir": "../other"},
                  headers=H).status_code == 400  # refused before anything is downloaded
    assert not (tmp_path / "escape").exists()


def test_fetch_allowlist(media, monkeypatch):
    mod, c, run = media
    for url in ("https://evil.example.com/x.jpg", "http://images.pexels.com/x.jpg",
                "https://user@images.pexels.com/x.jpg", "https://images.pexels.com:8443/x.jpg", "file:///etc/passwd"):
        r = c.post("/fetch", json={"run_id": RUN, "url": url}, headers=H)
        assert r.status_code == 400, url
    r = c.post("/embed/images", json={"run_id": RUN, "items": [{"url": "https://evil.example.com/x.jpg"}]},
               headers=H).json()
    assert not r["results"][0]["ok"] and "isn't allowed" in r["results"][0]["error"]

    # a stand-in for the CDN: a good image, a redirect elsewhere, and something that isn't an image
    seen: list[str] = []
    body = jpeg()

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path == "/photo.jpeg":
            return httpx.Response(200, content=body)
        if req.url.path == "/moved.jpeg":
            return httpx.Response(302, headers={"location": "https://evil.example.com/photo.jpeg"})
        return httpx.Response(200, content=b"<html>not an image</html>")

    real = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = c.post("/fetch", json={"run_id": RUN, "url": "https://images.pexels.com/photo.jpeg"}, headers=H).json()
    sha = hashlib.sha256(body).hexdigest()
    assert r == {"path": f"video/_cache/{sha}.jpg", "sha256": sha} and (run / r["path"]).read_bytes() == body
    n = len(seen)
    r = c.post("/fetch", json={"run_id": RUN, "url": "https://images.pexels.com/photo.jpeg", "sha256": sha},
               headers=H).json()
    assert r["path"] == f"video/_cache/{sha}.jpg" and len(seen) == n  # already here: not downloaded again
    assert c.post("/fetch", json={"run_id": RUN, "url": "https://images.pexels.com/moved.jpeg"},
                  headers=H).status_code == 400
    assert "evil.example.com" not in " ".join(seen)
    assert c.post("/fetch", json={"run_id": RUN, "url": "https://images.pexels.com/page.html"},
                  headers=H).status_code == 400


def test_fake_embeddings_are_normalized_and_deterministic(media):
    _, c, run = media
    a = c.post("/embed/text", json={"texts": ["hockey rink", "sunrise"]}, headers=H).json()
    b = c.post("/embed/text", json={"texts": ["hockey rink"]}, headers=H).json()
    assert a["dim"] == 512 and len(a["vectors"]) == 2 and a["vectors"][0] == b["vectors"][0]
    assert a["vectors"][0] != a["vectors"][1]
    for v in a["vectors"]:
        assert len(v) == 512 and math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-4)

    (run / "lib").mkdir()
    (run / "lib" / "red.png").write_bytes(png())
    (run / "lib" / "notes.txt").write_text("hello")
    r = c.post("/embed/images", json={"run_id": RUN, "items": [{"path": "lib/red.png"}, {"path": "lib/notes.txt"},
                                                               {"path": "lib/missing.png"}]}, headers=H).json()
    red, txt, missing = r["results"]
    assert red["ok"] and red["path"] == "lib/red.png" and (red["width"], red["height"]) == (800, 1200)
    assert math.isclose(math.sqrt(sum(x * x for x in red["vector"])), 1.0, abs_tol=1e-4)
    assert not txt["ok"] and "not a usable image" in txt["error"]
    (run / "lib" / "huge.png").write_bytes(png(size=(8000, 6000)))  # 48 MP
    huge = c.post("/embed/images", json={"run_id": RUN, "items": [{"path": "lib/huge.png"}]},
                  headers=H).json()["results"][0]
    assert not huge["ok"] and "too large" in huge["error"]
    assert not missing["ok"] and "no such file" in missing["error"]
    again = c.post("/embed/images", json={"run_id": RUN, "items": [
        {"path": "lib/red.png", "known_sha256": red["sha256"]}]}, headers=H).json()["results"][0]
    assert again["ok"] and again["unchanged"] and again["vector"] is None and again["sha256"] == red["sha256"]


def test_compose_writes_captioned_slides(media):
    _, c, run = media
    (run / "a.png").write_bytes(png(size=(800, 1200)))
    (run / "b.jpg").write_bytes(jpeg(size=(1920, 1080)))  # landscape: cover-cropped
    (run / "out").mkdir()
    (run / "out" / "07.png").write_bytes(png())  # left from an earlier render
    long = "Find a pickup hockey game near you tonight in under a minute, no group chat required, no signup at all"
    r = c.post("/slides/compose", json={"run_id": RUN, "out_dir": "out", "sheet": "preview.png", "slides": [
        {"image": "a.png", "caption": "Pickup hockey, tonight", "caption_position": "top"},
        {"image": "b.jpg", "caption": long, "caption_position": "bottom"},
        {"image": "a.png", "caption": ""}]}, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["slides"] == ["out/01.png", "out/02.png", "out/03.png"] and out["sheet"] == "preview.png"
    assert not (run / "out" / "07.png").exists()
    for p in out["slides"]:
        with Image.open(run / p) as im:
            assert im.format == "PNG" and im.size == (1080, 1920)
    # the caption is drawn in the safe zone: white/black pixels appear there and not below 70% of the height
    with Image.open(run / "out" / "02.png") as im:
        px = im.convert("RGB").load()
        rows = {y for y in range(0, 1920, 4) for x in range(0, 1080, 6) if px[x, y] in ((255, 255, 255), (0, 0, 0))}
        assert rows and min(rows) >= int(0.15 * 1920) - 4 and max(rows) <= int(0.70 * 1920) + 4
    with Image.open(run / "preview.png") as im:
        assert im.width > im.height and im.height > 1920 // 4
    r = c.post("/slides/compose", json={"run_id": RUN, "out_dir": "out", "slides": [{"image": "nope.png"}]},
               headers=H)
    assert r.status_code == 404


def test_contain_shows_the_whole_screenshot_clear_of_the_caption(media):
    _, c, run = media
    (run / "phone.png").write_bytes(png(color=(10, 200, 90), size=(1170, 2532)))  # taller than 9:16
    r = c.post("/slides/compose", json={"run_id": RUN, "out_dir": "s", "slides": [
        {"image": "phone.png", "caption": "Pickup games near you", "caption_position": "top", "fit": "contain"},
        {"image": "phone.png", "caption": "", "fit": "contain"},
        {"image": "phone.png", "caption": "Tap to join", "caption_position": "bottom", "fit": "contain"}]}, headers=H)
    assert r.status_code == 200, r.text
    for name, caption_at in (("01.png", "top"), ("02.png", None), ("03.png", "bottom")):
        with Image.open(run / "s" / name) as im:
            assert im.size == (1080, 1920)
            px = im.convert("RGB").load()
            green = [y for y in range(0, 1920, 2) if px[540, y] == (10, 200, 90)]
            text = [y for y in range(0, 1920, 2) for x in range(0, 1080, 4) if px[x, y] == (255, 255, 255)]
            assert green and max(green) - min(green) > 1000  # the screenshot, whole and large
            assert px[20, 20] != (10, 200, 90) and px[20, 20][1] < 120  # a darkened backdrop around it
            if caption_at == "top":
                assert text and max(text) < min(green)
            elif caption_at == "bottom":
                assert text and min(text) > max(green)
            else:
                assert not text


def ffprobe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,"
                        "width,height,pix_fmt,r_frame_rate,duration", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def tone(path: Path, seconds: float) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    str(path)], check=True)


def test_render_makes_an_mp4_as_long_as_the_slides(media):
    _, c, run = media
    for i, color in enumerate([(200, 30, 30), (30, 200, 30), (30, 30, 200)]):
        (run / f"{i}.png").write_bytes(png(color=color, size=(1080, 1920)))
    (run / "wide.jpg").write_bytes(jpeg(size=(1920, 1080)))  # not 9:16: cover-cropped like a slide
    for motion in ("kenburns", "none"):
        r = c.post("/render/slideshow", json={"run_id": RUN, "slides": ["0.png", "1.png", "2.png", "wide.jpg"],
                                              "durations_s": [1.5, 2.5, 2.0, 1.5], "out": "v/show.mp4",
                                              "motion": motion}, headers=H)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["path"] == "v/show.mp4" and out["size_bytes"] == (run / "v" / "show.mp4").stat().st_size
        info = ffprobe(run / "v" / "show.mp4")
        assert abs(float(info["format"]["duration"]) - 7.5) <= 0.2 and abs(out["duration_s"] - 7.5) <= 0.2
        (video,) = info["streams"]  # no music: no audio track
        assert (video["codec_name"], video["width"], video["height"], video["pix_fmt"], video["r_frame_rate"]) == \
            ("h264", 1080, 1920, "yuv420p", "30/1")
        assert sorted(p.name for p in (run / "v").iterdir()) == ["show.mp4"]  # the clips' temp folder is gone
    data = (run / "v" / "show.mp4").read_bytes()
    assert 0 < data.find(b"moov") < data.find(b"mdat")  # +faststart: the index comes first, so playback starts at once


def test_render_trims_the_music_to_the_video(media):
    _, c, run = media
    for i in range(3):
        (run / f"{i}.png").write_bytes(png(size=(1080, 1920)))
    tone(run / "song.wav", 12)
    r = c.post("/render/slideshow", json={"run_id": RUN, "slides": ["0.png", "1.png", "2.png"],
                                          "durations_s": [2, 2, 2.5], "out": "v/with-music.mp4", "music": "song.wav",
                                          "music_volume": 0.5}, headers=H)
    assert r.status_code == 200, r.text
    info = ffprobe(run / "v" / "with-music.mp4")
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert audio["codec_name"] == "aac" and abs(float(audio["duration"]) - 6.5) <= 0.2
    assert abs(float(video["duration"]) - 6.5) <= 0.2 and abs(float(info["format"]["duration"]) - 6.5) <= 0.2

    tone(run / "short.wav", 2)  # shorter than the video: looped, and the video keeps its length
    r = c.post("/render/slideshow", json={"run_id": RUN, "slides": ["0.png", "1.png", "2.png"],
                                          "durations_s": [2, 2, 2.5], "out": "v/loop.mp4", "music": "short.wav"},
               headers=H)
    assert r.status_code == 200, r.text
    assert abs(r.json()["duration_s"] - 6.5) <= 0.2


def test_render_refuses_bad_input(media, tmp_path):
    _, c, run = media
    (run / "a.png").write_bytes(png(size=(1080, 1920)))
    (tmp_path / "outside.png").write_bytes(png())
    os.symlink(tmp_path / "outside.png", run / "peek.png")
    # a "song" that is really a playlist pointing at another file: never opened as one
    (run / "song.mp3").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n")
    (run / "notes.txt").write_text("not an image")

    def render(**kw):
        return c.post("/render/slideshow", json={"run_id": RUN, "slides": ["a.png"], "durations_s": [2],
                                                 "out": "v/x.mp4", **kw}, headers=H)

    assert render(out="../../escape.mp4").status_code == 400
    assert render(out="v/x.gif").status_code == 400
    assert render(slides=["peek.png"]).status_code == 400
    assert render(slides=["missing.png"]).status_code == 404
    assert render(slides=["notes.txt"]).status_code == 400
    assert render(durations_s=[2, 2]).status_code == 400
    assert render(durations_s=[0.1]).status_code == 400
    assert render(width=1081).status_code == 400
    assert render(music="../../etc/passwd").status_code == 400
    r = render(music="song.mp3")
    assert r.status_code == 400 and "isn't an audio file" in r.json()["detail"]
    assert not (run / "v" / "x.mp4").exists() and not (tmp_path / "escape.mp4").exists()
    assert not any((run / "v").iterdir())  # nothing left behind, not even the clips' temp folder
    assert c.post("/render/slideshow", json={"run_id": RUN, "slides": ["a.png"], "durations_s": [2], "out": "v/x.mp4"},
                  headers={"X-Media-Token": "nope"}).status_code == 401
