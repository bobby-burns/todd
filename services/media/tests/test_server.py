"""The media server on a temp folder with fake embeddings (MEDIA_FAKE_EMBED=1): auth, confinement to the run's folder,
the fetch allowlist, embeddings and slide composition. Needs Pillow, numpy and the DejaVu fonts; no models, no network."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import math
import os
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
