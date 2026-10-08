"""Media server. Runs inside the isolated `media` container; only the API calls it, over its own network.

Endpoints (JSON, require X-Media-Token; every path is relative to the run's folder, WORKSPACE_ROOT/<run_id>):
  GET /health                                           -> {ok, image_model, text_model, dim}
  /embed/text     {texts}                               -> {model, image_model, dim, vectors}
  /embed/images   {run_id, items: [{url} | {path, known_sha256?}], save_dir}
                                                        -> {model, dim, results: [{ok, vector, sha256, width, height,
                                                            path, unchanged, error}]}
  /fetch          {run_id, url, save_dir, sha256?}      -> {path, sha256}
  /fetch/video    {run_id, url, out}                    -> {path, kind, duration_s, width, height, fps, size_bytes}
  /thumbs         {run_id, urls, labels, out}           -> {sheet, png_b64, missing}  (stock candidates, to pick by eye)
  /slides/compose {run_id, width, height, slides: [{image, caption, caption_position, fit}], out_dir, sheet?}
                                                        -> {slides: [path], sheet}
  /render/slideshow {run_id, slides: [path], durations_s, out, fps, motion, music?, music_volume, width, height}
                                                        -> {path, duration_s, size_bytes}
  /files/put      {run_id, path, data_b64}              -> {path, size_bytes, sha256}   (audio, image or video only)
  /probe          {run_id, paths}                       -> {items: [{path, kind, duration_s, width, height, fps}]}
  /render/timeline {run_id, out, fps, width, height, video, voice?, music?, captions, overlays}
                                                        -> {path, duration_s, frames, size_bytes}
  /screencast/put {run_id, session, frames: [{i, data_b64}]} -> {stored}   (JPEG frames of a browser recording)
  /tts/local      {run_id, text, out, speed}            -> {path, duration_s, alignment, voice}
  /frames         {run_id, src, out, count, delete_source} -> {sheet, duration_s, times}  (a reference's contact sheet)
  /shots          {run_id, src, out, threshold, inline, delete_source}
                                                        -> {shots: [{n, start, end, jump?}], cuts, jump_cuts,
                                                            avg_shot_s, longest_shot_s, sheets, images_b64?}
                                                           (a reference's edit: its cuts and a frame from every shot)
  /transcribe     {run_id, src}                         -> {text, words: [{word, start, end}]}  (local, free)
                                                           (the free scaffold voice)
  /screencast/assemble {run_id, session, times, end_s, out, fps}            -> {path, duration_s, frames, size_bytes}
It has no vault access, fetches only from MEDIA_FETCH_HOSTS over https, and reads and writes only inside run folders.
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import difflib
import hashlib
import hmac
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from pydantic import BaseModel, Field

ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
TOKEN = os.getenv("MEDIA_TOKEN", "")
TOKEN_FILE = os.getenv("MEDIA_TOKEN_FILE", "")  # a random token the API made (read-only here)
IMAGE_MODEL = os.getenv("MEDIA_IMAGE_MODEL", "Qdrant/clip-ViT-B-32-vision")
TEXT_MODEL = os.getenv("MEDIA_TEXT_MODEL", "Qdrant/clip-ViT-B-32-text")
MODELS_DIR = os.getenv("MEDIA_MODELS_DIR", "/models")  # downloaded at build time
FAKE_EMBED = os.getenv("MEDIA_FAKE_EMBED") == "1"  # tests: deterministic vectors, no model
DIM = int(os.getenv("MEDIA_DIM", "512"))
FETCH_DEFAULT = "images.pexels.com,videos.pexels.com,pixabay.com,cdn.pixabay.com"  # the stock sites Todd uses
FETCH_HOSTS = {h.strip().lower() for h in (os.getenv("MEDIA_FETCH_HOSTS") or FETCH_DEFAULT).split(",") if h.strip()}
FETCH_MAX = 20_000_000
FETCH_TIMEOUT = 20
USER_AGENT = "Todd-media/0.1 (+https://github.com/bobby-burns/todd)"  # image hosts turn away default client names
FONT = os.getenv("MEDIA_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
# On-screen text and captions in a short use the app's own typeface (TikTok Sans, OFL), so they read as typed in the app
TEXT_FONTS = Path(os.getenv("MEDIA_TEXT_FONTS", str(Path(__file__).resolve().parent / "fonts")))
TEXT_FONT, CAPTION_FONT = "TikTok Sans 36pt SemiBold", "TikTok Sans 36pt ExtraBold"
FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_PIXELS = 40_000_000  # larger images are refused before they're decoded (decompression bombs)
Image.MAX_IMAGE_PIXELS = 2 * MAX_PIXELS  # Pillow's own guard, as a backstop to the check in decode()

app = FastAPI(title="todd-media")


def _token() -> str:
    if TOKEN_FILE:
        try:
            return Path(TOKEN_FILE).read_text().strip()
        except OSError:
            return ""  # not made yet: refuse everything until it is
    return TOKEN


def auth(x_media_token: str = Header(default="")) -> None:
    token = _token()
    if not token or not hmac.compare_digest(x_media_token.encode(), token.encode()):
        raise HTTPException(401, "bad media token")


# ------------------------------------------------------------------------------------------ paths
def run_root(run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id or ""):
        raise HTTPException(400, "bad run_id")
    root = (ROOT / run_id).resolve()
    if ROOT not in root.parents:
        raise HTTPException(400, "path outside workspace")
    return root


def within(run_id: str, path: str) -> Path:
    """`path` inside the run's folder, symlinks resolved; anything that ends up outside it is refused."""
    root = run_root(run_id)
    p = (root / (path or "").lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "path outside this run's folder")
    return p


def rel(run_id: str, p: Path) -> str:
    return str(p.relative_to(run_root(run_id)))


# ------------------------------------------------------------------------------------------ images
def decode(data: bytes) -> tuple[Image.Image, str]:
    """(RGB image, file extension), or 400 when the bytes aren't a JPEG/PNG/WebP image."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise HTTPException(413, f"image too large ({probe.width}×{probe.height})")
            probe.verify()
        img = Image.open(io.BytesIO(data))
        fmt = img.format or ""
        img = ImageOps.exif_transpose(img)
        img.load()
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as e:
        raise HTTPException(400, f"not a usable image: {e}") from e
    if fmt not in FORMATS:
        raise HTTPException(400, f"unsupported image format {fmt or 'unknown'}")
    return img.convert("RGB"), FORMATS[fmt]


def _check_url(url: str) -> None:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
        raise HTTPException(400, "only plain https URLs can be fetched")
    if host not in FETCH_HOSTS:
        raise HTTPException(400, f"host {host or '?'} isn't allowed (MEDIA_FETCH_HOSTS)")


async def download(url: str, limit: int = FETCH_MAX, timeout: float = FETCH_TIMEOUT) -> bytes:
    """GET an allow-listed https URL (redirects only to allow-listed hosts), at most `limit` bytes."""
    _check_url(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT}) as c:
        for _ in range(5):
            try:
                async with asyncio.timeout(timeout):
                    async with c.stream("GET", url) as r:
                        if r.is_redirect:
                            url = urljoin(url, r.headers.get("location", ""))
                            _check_url(url)
                            continue
                        if r.status_code >= 400:
                            raise HTTPException(502, f"fetch failed: HTTP {r.status_code}")
                        if int(r.headers.get("content-length") or 0) > limit:
                            raise HTTPException(413, "file too large")
                        buf = bytearray()
                        async for chunk in r.aiter_bytes():
                            buf += chunk
                            if len(buf) > limit:
                                raise HTTPException(413, "file too large")
                        return bytes(buf)
            except (httpx.HTTPError, TimeoutError) as e:
                raise HTTPException(502, f"fetch failed: {type(e).__name__}") from e
    raise HTTPException(502, "too many redirects")


def save(run_id: str, save_dir: str, data: bytes, ext: str) -> tuple[str, str]:
    """Store the bytes as save_dir/<sha256>.<ext>; returns (path relative to the run folder, sha256)."""
    sha = hashlib.sha256(data).hexdigest()
    d = within(run_id, save_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = within(run_id, f"{rel(run_id, d)}/{sha}.{ext}")
    if not p.exists():
        tmp = within(run_id, rel(run_id, p) + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)
    return rel(run_id, p), sha


def cached(run_id: str, save_dir: str, sha: str) -> str | None:
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        return None
    for ext in FORMATS.values():
        p = within(run_id, f"{save_dir}/{sha}.{ext}")
        if p.is_file():
            return rel(run_id, p)
    return None


# ------------------------------------------------------------------------------------------ embeddings
class Embedder:
    """CLIP text and image vectors (fastembed, ONNX on CPU), L2-normalized. Models load on first use."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._image = self._text = None

    def _models(self):
        with self._lock:
            if self._image is None:
                from fastembed import ImageEmbedding, TextEmbedding

                self._image = ImageEmbedding(IMAGE_MODEL, cache_dir=MODELS_DIR)
                self._text = TextEmbedding(TEXT_MODEL, cache_dir=MODELS_DIR)
        return self._image, self._text

    def text(self, texts: list[str]) -> list[list[float]]:
        if FAKE_EMBED:
            return [fake_vector(t.encode()) for t in texts]
        _, model = self._models()
        return [unit(v) for v in model.embed(texts)]

    def images(self, images: list[Image.Image], raw: list[bytes]) -> list[list[float]]:
        if FAKE_EMBED:
            return [fake_vector(b) for b in raw]
        model, _ = self._models()
        return [unit(v) for v in model.embed(images)]


def unit(v) -> list[float]:
    a = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(a))
    return (a / n if n else a).round(6).tolist()


def fake_vector(data: bytes) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(data).digest()[:8], "big")
    return unit(np.random.default_rng(seed).standard_normal(DIM))


embedder = Embedder()


class TextReq(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=32)


@app.post("/embed/text", dependencies=[Depends(auth)])
async def embed_text(req: TextReq) -> dict:
    vectors = await asyncio.to_thread(embedder.text, [t[:1000] for t in req.texts])
    return {"model": TEXT_MODEL, "image_model": IMAGE_MODEL, "dim": DIM, "vectors": vectors}


class ImageItem(BaseModel):
    url: str | None = None
    path: str | None = None
    known_sha256: str | None = None  # a path item whose bytes still hash to this isn't embedded again


class ImagesReq(BaseModel):
    run_id: str
    items: list[ImageItem] = Field(min_length=1, max_length=40)
    save_dir: str = "video/_cache"


@app.post("/embed/images", dependencies=[Depends(auth)])
async def embed_images(req: ImagesReq) -> dict:
    within(req.run_id, req.save_dir)  # before any download
    sem = asyncio.Semaphore(6)

    async def load(item: ImageItem) -> dict:
        out: dict = {"ok": False, "vector": None, "sha256": None, "width": None, "height": None, "path": None,
                     "unchanged": False, "error": None}
        try:
            if item.url:
                async with sem:
                    data = await download(item.url)
                img, ext = decode(data)
                out["path"], out["sha256"] = save(req.run_id, req.save_dir, data, ext)
            elif item.path:
                p = within(req.run_id, item.path)
                if not p.is_file():
                    raise HTTPException(404, f"no such file: {item.path}")
                if p.stat().st_size > FETCH_MAX:
                    raise HTTPException(413, "image too large")
                data = p.read_bytes()
                img, _ = decode(data)
                out["path"], out["sha256"] = rel(req.run_id, p), hashlib.sha256(data).hexdigest()
            else:
                raise HTTPException(400, "each item needs a url or a path")
        except HTTPException as e:
            out["error"] = str(e.detail)
            return out
        out.update(ok=True, width=img.width, height=img.height, unchanged=out["sha256"] == item.known_sha256)
        out["_img"], out["_raw"] = img, data
        return out

    results = await asyncio.gather(*(load(i) for i in req.items))
    todo = [r for r in results if r["ok"] and not r["unchanged"]]
    if todo:
        vectors = await asyncio.to_thread(embedder.images, [r["_img"] for r in todo], [r["_raw"] for r in todo])
        for r, v in zip(todo, vectors):
            r["vector"] = v
    for r in results:
        r.pop("_img", None)
        r.pop("_raw", None)
    return {"model": IMAGE_MODEL, "dim": DIM, "results": results}


class FetchReq(BaseModel):
    run_id: str
    url: str
    save_dir: str = "video/_cache"
    sha256: str | None = None  # already in save_dir under this name: nothing is downloaded


@app.post("/fetch", dependencies=[Depends(auth)])
async def fetch(req: FetchReq) -> dict:
    within(req.run_id, req.save_dir)  # before any download
    have = cached(req.run_id, req.save_dir, req.sha256 or "")
    if have:
        return {"path": have, "sha256": req.sha256}
    data = await download(req.url)
    _, ext = decode(data)
    path, sha = save(req.run_id, req.save_dir, data, ext)
    return {"path": path, "sha256": sha}


VIDEO_FETCH_MAX = 60_000_000  # a stock clip (Pexels and Pixabay serve 720p–1080p files of a few MB)


class FetchVideoReq(BaseModel):
    run_id: str
    url: str
    out: str  # .mp4 path in the run folder


@app.post("/fetch/video", dependencies=[Depends(auth)])
async def fetch_video(req: FetchVideoReq) -> dict:
    """A stock video clip from an allow-listed host, checked to be a real video (FFprobe) before it's kept."""
    out = within(req.run_id, req.out)
    if out.suffix.lower() != ".mp4":
        raise HTTPException(400, "out must be an .mp4 path")
    data = await download(req.url, VIDEO_FETCH_MAX, timeout=90)

    def keep() -> dict:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part")
        tmp.write_bytes(data)
        try:
            video_format(tmp)  # refuses anything that isn't a plain video container
            info = media_info(req.run_id, tmp)
        except HTTPException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(out)
        return {**info, "path": rel(req.run_id, out), "size_bytes": len(data)}
    return await asyncio.to_thread(keep)


class ThumbsReq(BaseModel):
    run_id: str
    urls: list[str] = Field(min_length=1, max_length=16)
    labels: list[str] = Field(default_factory=list, max_length=16)
    out: str  # .png


@app.post("/thumbs", dependencies=[Depends(auth)])
async def thumbs(req: ThumbsReq) -> dict:
    """A labelled sheet of preview images (stock search results), for a model to choose from by eye."""
    out = within(req.run_id, req.out)
    if out.suffix.lower() != ".png":
        raise HTTPException(400, "out must be a .png path")
    got: list[bytes | None] = []
    for u in req.urls:
        try:
            got.append(await download(u))
        except HTTPException:
            got.append(None)

    def sheet() -> dict:
        font = ImageFont.truetype(FONT, 20)
        tiles = []
        for k, data in enumerate(got):
            label = req.labels[k] if k < len(req.labels) else str(k + 1)
            try:
                im, _ = decode(data) if data else (None, None)
            except HTTPException:
                im = None
            im = ImageOps.fit(im.convert("RGB"), (240, 320)) if im is not None else Image.new("RGB", (240, 320))
            tile = Image.new("RGB", (240, 352), (16, 16, 18))
            tile.paste(im, (0, 0))
            ImageDraw.Draw(tile).text((6, 326), label[:30], fill="white", font=font)
            tiles.append(tile)
        cols = min(4, len(tiles))
        rows = math.ceil(len(tiles) / cols)
        img = Image.new("RGB", (cols * 246 + 6, rows * 358 + 6), (0, 0, 0))
        for k, tile in enumerate(tiles):
            img.paste(tile, (6 + k % cols * 246, 6 + k // cols * 358))
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "PNG")
        return {"sheet": rel(req.run_id, out), "png_b64": base64.b64encode(out.read_bytes()).decode(),
                "missing": [k + 1 for k, d in enumerate(got) if d is None]}
    return await asyncio.to_thread(sheet)


# ------------------------------------------------------------------------------------------ slides
# Captions stay in TikTok's safe zone: its buttons and caption text cover the bottom and the right edge.
SAFE_TOP, SAFE_BOTTOM = 0.15, 0.70
ANCHORS = {"top": 0.18, "middle": 0.45, "bottom": 0.65}
MAX_LINES = 3


class Slide(BaseModel):
    image: str
    caption: str = ""
    caption_position: Literal["top", "middle", "bottom"] = "middle"
    # cover: fill the slide, cropping the overflow (photos). contain: the image framed on a blurred copy of itself and
    # clear of the caption, e.g. an app screenshot (a tall one keeps its top part: see contain()).
    fit: Literal["cover", "contain"] = "cover"


class ComposeReq(BaseModel):
    run_id: str
    width: int = Field(1080, ge=240, le=2160)
    height: int = Field(1920, ge=240, le=3840)
    slides: list[Slide] = Field(min_length=1, max_length=20)
    out_dir: str
    sheet: str | None = None  # also write every slide side by side into this one image


def cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale to fill w×h and crop the overflow, centered."""
    scale = max(w / img.width, h / img.height)
    size = (max(w, math.ceil(img.width * scale)), max(h, math.ceil(img.height * scale)))
    img = img.resize(size, Image.Resampling.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


KEEP_MIN = 0.55  # a tall image (a phone screenshot) shows at least this much of its height, from the top
FADE = 0.12  # and fades out over this share of what's shown
FRAME = (72, 72, 80)  # a thin outline, so a dark app screen stands out from its dark backdrop


def contain(img: Image.Image, w: int, h: int, caption_band: tuple[int, int] | None) -> Image.Image:
    """The image framed (rounded corners, thin outline) on a blurred and darkened copy of itself, in the largest band of
    the slide the caption leaves free. An image taller than the band at the band's width, like a phone screenshot,
    fills the width and keeps its top part (at least KEEP_MIN of its height), fading out at the bottom: an app screen's
    important part is at the top, and shown whole it would be too small to read."""
    bg = cover(img, max(1, w // 10), max(1, h // 10)).filter(ImageFilter.GaussianBlur(4))
    slide = ImageEnhance.Brightness(bg.resize((w, h), Image.Resampling.BICUBIC)).enhance(0.45)
    top, bottom = round(0.04 * h), round(0.94 * h)
    if caption_band:
        gap = round(0.025 * h)
        top, bottom = max([(top, caption_band[0] - gap), (caption_band[1] + gap, bottom)], key=lambda b: b[1] - b[0])
    line = max(1, round(0.003 * w))
    box_w, box_h = round(0.86 * w) - 2 * line, max(1, bottom - top - 2 * line)
    scale = min(box_w / img.width, box_h / img.height)
    if img.height * box_w / img.width > box_h:  # tall: as wide as KEEP_MIN allows, cropped at the bottom
        scale = min(box_w / img.width, box_h / (KEEP_MIN * img.height))
    fg = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
    cropped = fg.height > box_h
    if cropped:
        fg = fg.crop((0, 0, fg.width, box_h))
    framed = Image.new("RGB", (fg.width + 2 * line, fg.height + 2 * line), FRAME)
    framed.paste(fg, (line, line))
    mask = Image.new("L", framed.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, framed.width - 1, framed.height - 1), round(0.035 * w), fill=255)
    if cropped:  # fade out where it's cut off
        fade = max(1, round(FADE * framed.height))
        ramp = Image.linear_gradient("L").transpose(Image.Transpose.FLIP_TOP_BOTTOM).resize((framed.width, fade))
        zone = (0, framed.height - fade, framed.width, framed.height)
        mask.paste(ImageChops.multiply(mask.crop(zone), ramp), zone[:2])
    slide.paste(framed, ((w - framed.width) // 2, top + (bottom - top - framed.height) // 2), mask)
    return slide


def wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: float) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= max_w:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


class Caption:
    """A caption laid out for a w×h slide: TikTok style (bold, white, black outline), at most MAX_LINES lines, inside
    the safe zone."""

    def __init__(self, text: str, position: str, w: int, h: int) -> None:
        self.lines: list[str] = []
        self.w = w
        text = " ".join(text.split())
        if not text:
            return
        draw = ImageDraw.Draw(Image.new("L", (1, 1)))
        max_w = w * 0.80
        size = round(64 * w / 1080)
        while True:  # shrink until it fits in MAX_LINES
            font = ImageFont.truetype(FONT, size)
            lines = wrap(draw, text, font, max_w)
            if (len(lines) <= MAX_LINES and all(draw.textlength(ln, font=font) <= max_w for ln in lines)) \
                    or size <= round(40 * w / 1080):
                break
            size -= 4
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES]
            lines[-1] = lines[-1].rstrip(".,;:") + "…"
        self.font, self.lines = font, lines
        self.stroke = max(2, round(size * 6 / 64))
        self.line_h = round(size * 1.22)
        block = self.line_h * len(lines)
        top = round(ANCHORS.get(position, ANCHORS["middle"]) * h - block / 2)
        self.top = max(round(SAFE_TOP * h), min(top, round(SAFE_BOTTOM * h) - block))
        self.band = (self.top - self.stroke, self.top + block + self.stroke)

    def draw(self, img: Image.Image) -> None:
        draw = ImageDraw.Draw(img)
        for i, ln in enumerate(self.lines):
            draw.text((self.w / 2, self.top + i * self.line_h + self.line_h / 2), ln, font=self.font, fill="white",
                      anchor="mm", stroke_width=self.stroke, stroke_fill="black")


def contact_sheet(slides: list[Image.Image], cols: int = 6) -> Image.Image:
    tw, th = slides[0].width // 4, slides[0].height // 4
    gap = max(8, tw // 20)
    cols = min(cols, len(slides))
    rows = math.ceil(len(slides) / cols)
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * gap, rows * th + (rows + 1) * gap), (24, 24, 27))
    for i, s in enumerate(slides):
        r, c = divmod(i, cols)
        sheet.paste(s.resize((tw, th), Image.Resampling.LANCZOS), (gap + c * (tw + gap), gap + r * (th + gap)))
    return sheet


def _compose(req: ComposeReq) -> dict:
    out = within(req.run_id, req.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("[0-9][0-9].png"):  # a re-render with fewer slides leaves none behind
        old.unlink()
    paths, done = [], []
    for i, s in enumerate(req.slides, 1):
        src = within(req.run_id, s.image)
        if not src.is_file():
            raise HTTPException(404, f"no such file: {s.image}")
        img, _ = decode(src.read_bytes())
        cap = Caption(s.caption, s.caption_position, req.width, req.height)
        if s.fit == "contain":
            slide = contain(img, req.width, req.height, cap.band if cap.lines else None)
        else:
            slide = cover(img, req.width, req.height)
        cap.draw(slide)
        p = within(req.run_id, f"{rel(req.run_id, out)}/{i:02d}.png")
        slide.save(p, "PNG")
        paths.append(rel(req.run_id, p))
        done.append(slide)
    sheet = None
    if req.sheet:
        sp = within(req.run_id, req.sheet)
        sp.parent.mkdir(parents=True, exist_ok=True)
        contact_sheet(done).save(sp, "PNG")
        sheet = rel(req.run_id, sp)
    return {"slides": paths, "sheet": sheet}


@app.post("/slides/compose", dependencies=[Depends(auth)])
async def compose(req: ComposeReq) -> dict:
    return await asyncio.to_thread(_compose, req)


# ------------------------------------------------------------------------------------------ video
FFMPEG = os.getenv("MEDIA_FFMPEG", "ffmpeg")
FFPROBE = os.getenv("MEDIA_FFPROBE", "ffprobe")
FFMPEG_TIMEOUT = 300
ZOOM = 0.06  # Ken Burns: how far a slide zooms in (or out) over its time on screen
MUSIC_MAX = 50_000_000
# Music is opened only with one of these demuxers, named explicitly, and FFmpeg may open only local files: a "music"
# file that is really a playlist (HLS, concat) can't make it read other files or reach the network.
AUDIO_FORMATS = {"mp3": "mp3", "mov,mp4,m4a,3gp,3g2,mj2": "mov", "wav": "wav", "ogg": "ogg", "flac": "flac",
                 "aac": "aac"}
LOCAL_ONLY = ("-protocol_whitelist", "file")
_renders = threading.BoundedSemaphore(2)  # FFmpeg uses every core: two renders at a time at most


class RenderReq(BaseModel):
    run_id: str
    slides: list[str] = Field(min_length=1, max_length=20)  # images, shown in order (cover-cropped to width×height)
    durations_s: list[float] = Field(min_length=1, max_length=20)
    out: str  # an .mp4 path
    fps: int = Field(30, ge=12, le=60)
    motion: Literal["kenburns", "none"] = "kenburns"
    music: str | None = None  # an audio file in the run folder: looped or trimmed to the video, faded out at the end
    music_volume: float = Field(0.8, ge=0, le=2)
    width: int = Field(1080, ge=240, le=2160)
    height: int = Field(1920, ge=240, le=3840)


def ffmpeg(*args: str, timeout: float = FFMPEG_TIMEOUT, cwd: Path | None = None) -> None:
    """Run FFmpeg with an argument list (never a shell)."""
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
                           capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError as e:
        raise HTTPException(500, "ffmpeg isn't installed in the media container") from e
    except subprocess.TimeoutExpired as e:
        raise HTTPException(504, f"ffmpeg took longer than {timeout:g}s") from e
    if r.returncode != 0:
        raise HTTPException(500, "ffmpeg failed: " + " | ".join(r.stderr.strip().splitlines()[-3:])[:600])


def probe(path: Path, *args: str) -> dict:
    try:
        r = subprocess.run([FFPROBE, "-v", "error", *LOCAL_ONLY, *args, "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise HTTPException(500, "ffprobe isn't installed in the media container") from e
    except subprocess.TimeoutExpired as e:
        raise HTTPException(504, "ffprobe timed out") from e
    if r.returncode != 0:
        raise HTTPException(400, f"can't read {path.name}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout or "{}")


def audio_format(path: Path) -> str:
    """The demuxer to open a music file with, or 400 when it isn't an audio file in a format on AUDIO_FORMATS."""
    demuxers = ",".join(f.split(",")[0] for f in AUDIO_FORMATS)
    try:
        info = probe(path, "-format_whitelist", demuxers, "-show_entries", "format=format_name:stream=codec_type")
    except HTTPException as e:
        raise HTTPException(400, f"{path.name} isn't an audio file (mp3, m4a, aac, wav, ogg or flac)") from e
    name = (info.get("format") or {}).get("format_name", "")
    if name not in AUDIO_FORMATS or not any(s.get("codec_type") == "audio" for s in info.get("streams") or []):
        raise HTTPException(400, f"{path.name} isn't an audio file (mp3, m4a, aac, wav, ogg or flac)")
    return AUDIO_FORMATS[name]


def duration(path: Path) -> float:
    return float((probe(path, "-show_entries", "format=duration").get("format") or {}).get("duration") or 0)


def clip_args(i: int, frame: Path, frames: int, req: RenderReq) -> list[str]:
    """FFmpeg arguments for one slide's clip: `frames` frames of the image, zooming slowly (in on odd slides, out on
    even ones) unless motion is "none"."""
    w, h, fps = req.width, req.height, req.fps
    if req.motion == "kenburns":
        # one input frame, `frames` output frames; upscaling first keeps zoompan's whole-pixel crop from jittering
        t = f"on/{max(frames - 1, 1)}"
        z = f"1+{ZOOM}*{t}" if i % 2 else f"{1 + ZOOM}-{ZOOM}*{t}"
        vf = (f"scale={2 * w}:{2 * h},zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={frames}"
              f":s={w}x{h}:fps={fps},format=yuv420p")
        source = [*LOCAL_ONLY, "-i", str(frame)]
    else:
        vf = "format=yuv420p"
        source = [*LOCAL_ONLY, "-loop", "1", "-framerate", str(fps), "-i", str(frame)]
    return [*source, "-vf", vf, "-frames:v", str(frames), "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-g", str(2 * fps), "-pix_fmt", "yuv420p", "-an"]


def _render(req: RenderReq) -> dict:
    if len(req.durations_s) != len(req.slides):
        raise HTTPException(400, "one duration per slide")
    if any(not 0.5 <= d <= 30 for d in req.durations_s):
        raise HTTPException(400, "each slide is on screen 0.5–30 seconds")
    if req.width % 2 or req.height % 2:
        raise HTTPException(400, "width and height must be even")
    out = within(req.run_id, req.out)
    if out.suffix.lower() != ".mp4":
        raise HTTPException(400, "out must be an .mp4 path")
    sources = []
    for s in req.slides:
        p = within(req.run_id, s)
        if not p.is_file():
            raise HTTPException(404, f"no such file: {s}")
        sources.append(p)
    music, music_fmt = None, ""
    if req.music:
        music = within(req.run_id, req.music)
        if not music.is_file():
            raise HTTPException(404, f"no such file: {req.music}")
        if music.stat().st_size > MUSIC_MAX:
            raise HTTPException(413, "music file too large")
        music_fmt = audio_format(music)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".render-", dir=out.parent))  # in the run folder, removed below
    try:
        with _renders:
            clips, frames_total = [], 0
            for i, (src, d) in enumerate(zip(sources, req.durations_s), 1):
                img, _ = decode(src.read_bytes())  # FFmpeg only ever sees images this server wrote
                frame = tmp / f"slide_{i:02d}.png"
                cover(img, req.width, req.height).save(frame, "PNG")
                frames = max(1, round(d * req.fps))
                frames_total += frames
                clip = tmp / f"clip_{i:02d}.mp4"
                ffmpeg(*clip_args(i, frame, frames, req), str(clip))
                clips.append(clip)
            listing = tmp / "clips.txt"
            listing.write_text("".join(f"file '{c.name}'\n" for c in clips))
            total = frames_total / req.fps
            args = [*LOCAL_ONLY, "-f", "concat", "-i", str(listing)]
            if music:
                args += [*LOCAL_ONLY, "-f", music_fmt, "-stream_loop", "-1", "-i", str(music),
                         "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                         "-af", f"volume={req.music_volume:g},afade=t=out:st={max(0.0, total - 1):.3f}:d=1",
                         "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}", "-shortest"]
            else:
                args += ["-c", "copy"]
            final = tmp / "final.mp4"
            ffmpeg(*args, "-movflags", "+faststart", str(final))
            final.replace(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"path": rel(req.run_id, out), "duration_s": round(duration(out), 3), "size_bytes": out.stat().st_size}


@app.post("/render/slideshow", dependencies=[Depends(auth)])
async def render(req: RenderReq) -> dict:
    return await asyncio.to_thread(_render, req)


# ------------------------------------------------------------------------------------------ timeline (shorts)
# A short is planned by the API (todd/shorts_plan.py) from the voiceover's character timestamps; this renders the plan.
# Sync rule: video segments are rendered to exact frame counts and joined video-only, the voice is ONE track placed by
# sample offsets, and captions are burned in from the same plan in the final pass. Audio is never cut into per-segment
# files and concatenated (that's where AAC padding gaps and drift come from).
VIDEO_FORMATS = {"mov,mp4,m4a,3gp,3g2,mj2": "mov", "matroska,webm": "matroska"}
PUT_MAX = 25_000_000
PUT_KINDS = {".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".ogg": "audio", ".flac": "audio",
             ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
             ".mp4": "video", ".mov": "video", ".webm": "video", ".m4v": "video"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
SAMPLE_RATE = 48000
CAPTION_Y = 0.66  # caption baseline, as a share of the height: inside TikTok's safe zone (15–70%)
HIGHLIGHT = "&H0000E5FF&"  # the spoken word: warm yellow (ASS colours are &HAABBGGRR)


def video_format(path: Path) -> str:
    """The demuxer to open a clip with, or 400 when it isn't an MP4/MOV/WebM/MKV video."""
    demuxers = ",".join(f.split(",")[0] for f in VIDEO_FORMATS)
    try:
        info = probe(path, "-format_whitelist", demuxers, "-show_entries", "format=format_name:stream=codec_type")
    except HTTPException as e:
        raise HTTPException(400, f"{path.name} isn't a video (mp4, mov, webm)") from e
    name = (info.get("format") or {}).get("format_name", "")
    if name not in VIDEO_FORMATS or not any(st.get("codec_type") == "video" for st in info.get("streams") or []):
        raise HTTPException(400, f"{path.name} isn't a video (mp4, mov, webm)")
    return VIDEO_FORMATS[name]


def media_info(run_id: str, p: Path) -> dict:
    if not p.is_file():
        raise HTTPException(404, f"no such file: {rel(run_id, p)}")
    out: dict = {"path": rel(run_id, p)}
    if p.suffix.lower() in IMAGE_EXT:
        img, _ = decode(p.read_bytes())
        return {**out, "kind": "image", "width": img.width, "height": img.height, "duration_s": None, "fps": None}
    if PUT_KINDS.get(p.suffix.lower()) == "audio":
        audio_format(p)
        return {**out, "kind": "audio", "duration_s": round(duration(p), 3), "width": None, "height": None,
                "fps": None}
    fmt = video_format(p)
    info = probe(p, "-f", fmt, "-select_streams", "v:0", "-show_entries",
                 "stream=width,height,avg_frame_rate:format=duration")
    st = (info.get("streams") or [{}])[0]
    num, _, den = str(st.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return {**out, "kind": "video", "width": st.get("width"), "height": st.get("height"), "fps": round(fps, 3),
            "duration_s": round(float((info.get("format") or {}).get("duration") or 0), 3)}


class PutReq(BaseModel):
    run_id: str
    path: str
    data_b64: str = Field(max_length=PUT_MAX * 4 // 3 + 8)


def _put(req: PutReq) -> dict:
    p = within(req.run_id, req.path)
    kind = PUT_KINDS.get(p.suffix.lower())
    if not kind:
        raise HTTPException(400, f"only {', '.join(sorted(PUT_KINDS))} files")
    try:
        data = base64.b64decode(req.data_b64, validate=True)
    except ValueError as e:
        raise HTTPException(400, "data_b64 isn't base64") from e
    if len(data) > PUT_MAX:
        raise HTTPException(413, "file too large")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.part")
    tmp.write_bytes(data)
    try:  # it must really be what its name says
        if kind == "image":
            decode(data)
        elif kind == "audio":
            audio_format(tmp)
        else:
            video_format(tmp)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(p)
    return {"path": rel(req.run_id, p), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@app.post("/files/put", dependencies=[Depends(auth)])
async def put_file(req: PutReq) -> dict:
    return await asyncio.to_thread(_put, req)


class ProbeReq(BaseModel):
    run_id: str
    paths: list[str] = Field(min_length=1, max_length=80)


@app.post("/probe", dependencies=[Depends(auth)])
async def probe_files(req: ProbeReq) -> dict:
    def go() -> dict:
        items = []
        for path in req.paths:
            try:
                items.append({"ok": True, **media_info(req.run_id, within(req.run_id, path))})
            except HTTPException as e:
                items.append({"ok": False, "path": path, "error": str(e.detail)})
        return {"items": items}
    return await asyncio.to_thread(go)


class TLCrop(BaseModel):
    x: float = Field(0.5, ge=0, le=1)  # the region's centre, as shares of the frame
    y: float = Field(0.5, ge=0, le=1)
    zoom: float = Field(1.3, ge=1, le=3)


class TLVideo(BaseModel):
    kind: Literal["clip", "image", "placeholder"]
    frames: int = Field(ge=1, le=60 * 120)
    src: str | None = None
    in_s: float = Field(0.0, ge=0)
    speed: float = Field(1.0, ge=0.25, le=4)
    freeze_s: float = Field(0.0, ge=0, le=10)
    hold_s: float = Field(0.0, ge=0, le=10)  # the first frame held this long before the clip plays (screen recordings)
    fit: Literal["cover", "contain"] = "cover"
    zoom: Literal["in", "out", "none"] = "none"
    label: str = Field("", max_length=400)  # placeholders: what will be generated here (animatic)
    crop: TLCrop | None = None  # a punch-in: the frame cut down to a region around (x, y), scaled back up
    grade: Literal["none", "phone"] = "none"  # "phone": stock or generated footage toned down to look phone-shot


class TLSegment(BaseModel):
    src_in: float = Field(ge=0)
    src_out: float = Field(gt=0)
    at: float = Field(ge=0)


class TLVoice(BaseModel):
    src: str
    segments: list[TLSegment] = Field(min_length=1, max_length=60)


class TLMusic(BaseModel):
    src: str
    volume: float = Field(0.25, ge=0, le=1)


class TLWord(BaseModel):
    text: str = Field(max_length=60)
    start: float = Field(ge=0)
    end: float = Field(ge=0)


class TLCaption(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    words: list[TLWord] = Field(min_length=1, max_length=8)


class TLOverlay(BaseModel):
    text: str = Field(max_length=120)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    position: Literal["top", "middle", "low", "tag"] = "top"  # tag: a small label in the corner (scaffold beat ids)
    style: Literal["box", "outline"] = "box"  # the app's two text looks: a white box, or white with a black edge


class TimelineReq(BaseModel):
    run_id: str
    out: str
    fps: int = Field(30, ge=12, le=60)
    width: int = Field(1080, ge=240, le=2160)
    height: int = Field(1920, ge=240, le=3840)
    video: list[TLVideo] = Field(min_length=1, max_length=120)  # quick cuts and punch-ins: up to ~4 pieces a beat
    voice: TLVoice | None = None
    music: TLMusic | None = None
    captions: list[TLCaption] = Field(default_factory=list, max_length=200)
    caption_style: Literal["phrase", "karaoke"] = "karaoke"  # phrase: the words of a page shown together, no colour pop
    overlays: list[TLOverlay] = Field(default_factory=list, max_length=120)


def ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(t: str) -> str:
    return " ".join(t.replace("\\", "/").replace("{", "(").replace("}", ")").split())


OVERLAY_Y = {"top": 0.17, "middle": 0.42, "low": 0.56}  # where on-screen text sits (its top, as a share of height)


def ass_script(req: TimelineReq) -> str:
    """Captions and on-screen text as an ASS script for libass, in the app's typeface: captions white with a black
    edge (the spoken word highlighted in karaoke style, or a whole phrase at once), on-screen text in a white box or
    white with a black edge, all inside the platform's safe zone."""
    w, h = req.width, req.height
    cap, box = round(84 * w / 1080), round(78 * w / 1080)  # TikTok Sans draws small for its size: these read like the app
    edge = max(2, round(box * 0.09))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        # captions: white, black outline, bottom-anchored at CAPTION_Y
        f"Style: Caption,{CAPTION_FONT},{cap},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,"
        f"{max(3, round(cap * 0.1))},0,2,{round(w * 0.1)},{round(w * 0.1)},{round(h * (1 - CAPTION_Y))},1",
    ]
    for pos, y in OVERLAY_Y.items():
        # box: black text on a white box (BorderStyle 3 draws OutlineColour as the box); outline: white, black edge
        lines += [
            f"Style: Box-{pos},{TEXT_FONT},{box},&H00000000,&H00000000,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,3,"
            f"{round(box * 0.26)},0,8,{round(w * 0.1)},{round(w * 0.1)},{round(h * y)},1",
            f"Style: Outline-{pos},{TEXT_FONT},{box},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,"
            f"0,1,{edge},0,8,{round(w * 0.1)},{round(w * 0.1)},{round(h * y)},1",
        ]
    lines += [
        # beat labels on a scaffold: small, bottom-left, clear of captions and text boxes
        f"Style: Tag,DejaVu Sans,{round(28 * w / 1080)},&H00FFFFFF,&H00FFFFFF,&H90000000,&H00000000,-1,0,0,0,100,100,"
        f"0,0,3,{round(8 * w / 1080)},0,1,{round(w * 0.04)},{round(w * 0.04)},{round(h * 0.03)},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for o in req.overlays:
        if o.end > o.start and ass_text(o.text):
            style = "Tag" if o.position == "tag" else f"{o.style.capitalize()}-{o.position}"
            lines.append(f"Dialogue: 1,{ass_time(o.start)},{ass_time(o.end)},{style},,0,0,0,,{ass_text(o.text)}")
    for c in req.captions:
        words = [ass_text(wd.text) for wd in c.words]
        if req.caption_style == "phrase":
            if c.end > c.start:
                lines.append(f"Dialogue: 0,{ass_time(c.start)},{ass_time(c.end)},Caption,,0,0,0,,{' '.join(words)}")
            continue
        for i, wd in enumerate(c.words):  # one event per spoken word, that word highlighted
            start = c.start if i == 0 else wd.start
            end = c.words[i + 1].start if i + 1 < len(c.words) else c.end
            if end <= start:
                continue
            text = " ".join(f"{{\\c{HIGHLIGHT}}}{t}{{\\c&H00FFFFFF&}}" if j == i else t for j, t in enumerate(words))
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def still_args(frame: Path, frames: int, w: int, h: int, fps: int, zoom: str) -> list[str]:
    if zoom == "none":
        return [*LOCAL_ONLY, "-loop", "1", "-framerate", str(fps), "-i", str(frame), "-vf", "format=yuv420p"]
    t = f"on/{max(frames - 1, 1)}"
    z = f"1+{ZOOM}*{t}" if zoom == "in" else f"{1 + ZOOM}-{ZOOM}*{t}"
    return [*LOCAL_ONLY, "-i", str(frame), "-vf",
            f"scale={2 * w}:{2 * h},zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={frames}"
            f":s={w}x{h}:fps={fps},format=yuv420p"]


def placeholder(img: Image.Image | None, label: str, w: int, h: int) -> Image.Image:
    """An animatic stand-in for a shot that isn't generated yet: its start frame full-bleed, as the clip will be (or a
    plain card), and what it'll be."""
    slide = cover(img, w, h) if img is not None else Image.new("RGB", (w, h), (28, 30, 38))
    draw = ImageDraw.Draw(slide, "RGBA")
    font = ImageFont.truetype(FONT, round(30 * w / 1080))
    lines = wrap(draw, f"AI SHOT · {label}", font, w * 0.84)[:4]
    pad, lh = round(w * 0.03), round(30 * w / 1080 * 1.3)
    top = round(h * 0.03)
    draw.rounded_rectangle((round(w * 0.05), top, round(w * 0.95), top + 2 * pad + lh * len(lines)), round(w * 0.02),
                           fill=(0, 0, 0, 170), outline=(255, 214, 0, 255), width=max(2, round(w * 0.003)))
    for i, ln in enumerate(lines):
        draw.text((round(w * 0.05) + pad, top + pad + i * lh), ln, font=font, fill=(255, 214, 0))
    return slide


def crop_filter(crop: "TLCrop | None", w: int, h: int) -> str:
    """A punch-in on a frame already at w×h: the region around (x, y), 1/zoom of the frame, scaled back to w×h."""
    if crop is None or crop.zoom <= 1.001:
        return ""
    cw, ch = 2 * round(w / crop.zoom / 2), 2 * round(h / crop.zoom / 2)
    x = min(max(round(crop.x * w - cw / 2), 0), w - cw)
    y = min(max(round(crop.y * h - ch / 2), 0), h - ch)
    return f",crop={cw}:{ch}:{x}:{y},scale={w}:{h}:flags=lanczos"


GRADES = {"none": "", "phone": ",eq=saturation=0.88:contrast=0.96,noise=alls=5:allf=t"}


def fit_filter(fit: str, w: int, h: int) -> str:
    if fit == "contain":  # the whole frame on a blurred, darkened copy of itself
        return (f"split[a][b];[a]scale={w // 8}:{h // 8}:force_original_aspect_ratio=increase,crop={w // 8}:{h // 8},"
                f"boxblur=4,scale={w}:{h},eq=brightness=-0.25[bg];"
                f"[b]scale={round(w * 0.9)}:{round(h * 0.9)}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


ENCODE = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p")


def _segment(req: TimelineReq, i: int, item: TLVideo, tmp: Path) -> Path:
    w, h, fps = req.width, req.height, req.fps
    out = tmp / f"seg_{i:02d}.mp4"
    if item.kind == "clip":
        src = within(req.run_id, item.src or "")
        if not src.is_file():
            raise HTTPException(404, f"no such file: {item.src}")
        if src.suffix.lower() in IMAGE_EXT:
            raise HTTPException(400, f"{item.src} is an image: use kind=image")
        fmt = video_format(src)
        graph = (f"[0:v]setpts=(PTS-STARTPTS)/{item.speed:g},fps={fps},{fit_filter(item.fit, w, h)}"
                 f"{crop_filter(item.crop, w, h)}{GRADES[item.grade]},"
                 f"tpad=start_mode=clone:start_duration={item.hold_s:.3f}:"
                 f"stop_mode=clone:stop_duration={item.freeze_s + 1:.3f},format=yuv420p[v]")
        ffmpeg(*LOCAL_ONLY, "-f", fmt, "-ss", f"{item.in_s:.3f}", "-i", str(src), "-filter_complex", graph,
               "-map", "[v]", "-frames:v", str(item.frames), "-r", str(fps), *ENCODE, "-an", str(out))
        return out
    img = None
    if item.src:
        src = within(req.run_id, item.src)
        if not src.is_file():
            raise HTTPException(404, f"no such file: {item.src}")
        img, _ = decode(src.read_bytes())
    if item.kind == "placeholder":
        still = placeholder(img, item.label, w, h)
    elif img is None:
        raise HTTPException(400, f"video item {i + 1}: an image needs a src")
    else:
        still = contain(img, w, h, None) if item.fit == "contain" else cover(img, w, h)
        if item.crop is not None and item.crop.zoom > 1.001:
            cw, ch = round(w / item.crop.zoom), round(h / item.crop.zoom)
            x = min(max(round(item.crop.x * w - cw / 2), 0), w - cw)
            y = min(max(round(item.crop.y * h - ch / 2), 0), h - ch)
            still = still.crop((x, y, x + cw, y + ch)).resize((w, h), Image.LANCZOS)
    frame = tmp / f"still_{i:02d}.png"
    still.save(frame, "PNG")
    ffmpeg(*still_args(frame, item.frames, w, h, fps, item.zoom), "-frames:v", str(item.frames), "-r", str(fps),
           *ENCODE, "-an", str(out))
    return out


def _audio(req: TimelineReq, total: float, tmp: Path) -> Path:
    """The soundtrack as one WAV of exactly `total` seconds: the voice segments placed by sample offsets, any music
    ducked under them."""
    out = tmp / "audio.wav"
    inputs: list[str] = []
    graph: list[str] = []
    mix = None
    if req.voice:
        src = within(req.run_id, req.voice.src)
        if not src.is_file():
            raise HTTPException(404, f"no such file: {req.voice.src}")
        inputs += [*LOCAL_ONLY, "-f", audio_format(src), "-i", str(src)]
        n = len(req.voice.segments)
        graph.append(f"[0:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,asplit={n}"
                     + "".join(f"[r{k}]" for k in range(n)))
        for k, sg in enumerate(req.voice.segments):
            if sg.src_out <= sg.src_in:
                raise HTTPException(400, f"voice segment {k + 1}: src_out must be after src_in")
            # asetpts=N/SR/TB after adelay: timestamps rebuilt from the sample count, or later filters (apad, atrim)
            # see the delay's timestamps and drop it (measured: the voice landed 1 s early without it)
            graph.append(f"[r{k}]atrim=start={sg.src_in:.4f}:end={sg.src_out:.4f},asetpts=PTS-STARTPTS,"
                         f"adelay=delays={round(sg.at * 1000)}:all=1,asetpts=N/SR/TB[s{k}]")
        graph.append("".join(f"[s{k}]" for k in range(n))
                     + f"amix=inputs={n}:normalize=0:dropout_transition=0,asetpts=N/SR/TB[voice]")
        mix = "voice"
    if req.music:
        src = within(req.run_id, req.music.src)
        if not src.is_file():
            raise HTTPException(404, f"no such file: {req.music.src}")
        idx = 1 if req.voice else 0
        inputs += [*LOCAL_ONLY, "-f", audio_format(src), "-stream_loop", "-1", "-i", str(src)]
        graph.append(f"[{idx}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                     f"volume={req.music.volume:g}[music]")
        if mix:  # duck the music whenever the voice speaks
            graph += ["[voice]asplit=2[vk][vm]",
                      "[music][vk]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[duck]",
                      "[vm][duck]amix=inputs=2:normalize=0:dropout_transition=0,asetpts=N/SR/TB[mixed]"]
            mix = "mixed"
        else:
            mix = "music"
    if not mix:  # silent, so every short has an audio track
        inputs += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
        mix = "0:a"
    graph.append(f"[{mix}]apad=whole_dur={total:.4f},atrim=0:{total:.4f}[out]")
    ffmpeg(*inputs, "-filter_complex", ";".join(graph), "-map", "[out]", "-c:a", "pcm_s16le", "-ar",
           str(SAMPLE_RATE), str(out))
    return out


def _timeline(req: TimelineReq) -> dict:
    if req.width % 2 or req.height % 2:
        raise HTTPException(400, "width and height must be even")
    out = within(req.run_id, req.out)
    if out.suffix.lower() != ".mp4":
        raise HTTPException(400, "out must be an .mp4 path")
    frames = sum(v.frames for v in req.video)
    total = frames / req.fps
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".render-", dir=out.parent))
    try:
        with _renders:
            segs = [_segment(req, i, v, tmp) for i, v in enumerate(req.video)]
            (tmp / "segs.txt").write_text("".join(f"file '{p.name}'\n" for p in segs))
            ffmpeg(*LOCAL_ONLY, "-f", "concat", "-i", "segs.txt", "-c", "copy", "video.mp4", cwd=tmp)
            _audio(req, total, tmp)
            vf: list[str] = []
            if req.captions or req.overlays:
                (tmp / "captions.ass").write_text(ass_script(req), encoding="utf-8")
                fonts = tmp / "fonts"  # the app's typeface for text, DejaVu for scaffold labels
                fonts.mkdir()
                for f in [*TEXT_FONTS.glob("*.ttf"), Path(FONT)]:
                    if f.is_file():
                        shutil.copy(f, fonts / f.name)
                vf = ["-vf", "ass=captions.ass:fontsdir=fonts"]
            ffmpeg(*LOCAL_ONLY, "-i", "video.mp4", "-i", "audio.wav", *vf, "-map", "0:v", "-map", "1:a",
                   "-frames:v", str(frames), "-r", str(req.fps), *ENCODE[:4], "-crf", "19", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k", "-ar", str(SAMPLE_RATE), "-t", f"{total:.4f}",
                   "-movflags", "+faststart", "final.mp4", cwd=tmp)
            (tmp / "final.mp4").replace(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"path": rel(req.run_id, out), "duration_s": round(duration(out), 3), "frames": frames,
            "size_bytes": out.stat().st_size}


@app.post("/render/timeline", dependencies=[Depends(auth)])
async def render_timeline(req: TimelineReq) -> dict:
    return await asyncio.to_thread(_timeline, req)


# ------------------------------------------------------------------------------------------ screencasts
# The API records the product in the agents' browser (todd/screencast.py) and hands the JPEG frames over in chunks; the
# browser sends a frame only when the page changes, so each frame is held until the next one's timestamp, which keeps
# the recording's step times (the sync points) exact in the 30 fps MP4.
CAST_ID = re.compile(r"[a-z0-9]{8,32}")
CAST_DIR = ".casts"  # inside the run folder, removed once assembled
CAST_FRAME_MAX = 3_000_000


def cast_dir(run_id: str, session: str) -> Path:
    if not CAST_ID.fullmatch(session or ""):
        raise HTTPException(400, "bad session id")
    return within(run_id, f"{CAST_DIR}/{session}")


class CastFrame(BaseModel):
    i: int = Field(ge=0, le=20000)
    data_b64: str = Field(max_length=CAST_FRAME_MAX * 4 // 3 + 8)


class CastPutReq(BaseModel):
    run_id: str
    session: str
    frames: list[CastFrame] = Field(min_length=1, max_length=120)


def _cast_put(req: CastPutReq) -> dict:
    d = cast_dir(req.run_id, req.session)
    d.mkdir(parents=True, exist_ok=True)
    for f in req.frames:
        try:
            data = base64.b64decode(f.data_b64, validate=True)
            with Image.open(io.BytesIO(data)) as im:
                if im.format != "JPEG" or im.width * im.height > MAX_PIXELS:
                    raise HTTPException(400, f"frame {f.i}: not a JPEG frame")
                im.verify()
        except (ValueError, OSError, SyntaxError) as e:
            raise HTTPException(400, f"frame {f.i}: not a JPEG frame") from e
        if len(data) > CAST_FRAME_MAX:
            raise HTTPException(413, f"frame {f.i} too large")
        (d / f"{f.i:05d}.jpg").write_bytes(data)
    return {"stored": len(req.frames)}


@app.post("/screencast/put", dependencies=[Depends(auth)])
async def cast_put(req: CastPutReq) -> dict:
    return await asyncio.to_thread(_cast_put, req)


class CastAssembleReq(BaseModel):
    run_id: str
    session: str
    times: list[float] = Field(min_length=1, max_length=20001)  # seconds from the start, one per frame, ascending
    end_s: float = Field(gt=0, le=600)
    out: str
    fps: int = Field(30, ge=12, le=60)


def _cast_assemble(req: CastAssembleReq) -> dict:
    d = cast_dir(req.run_id, req.session)
    out = within(req.run_id, req.out)
    if out.suffix.lower() != ".mp4":
        raise HTTPException(400, "out must be an .mp4 path")
    try:
        files = sorted(d.glob("*.jpg"))
        if len(files) != len(req.times):
            raise HTTPException(400, f"{len(files)} frames stored but {len(req.times)} times given")
        if any(b < a for a, b in zip(req.times, req.times[1:])) or req.end_s < req.times[-1]:
            raise HTTPException(400, "times must ascend and end before end_s")
        starts = [0.0, *req.times[1:]]  # the first frame covers the start, so step times stay true
        lines = []
        for k, f in enumerate(files):
            dur = (starts[k + 1] if k + 1 < len(files) else req.end_s) - starts[k]
            lines += [f"file '{f.name}'", f"duration {max(dur, 0.001):.4f}"]
        lines.append(f"file '{files[-1].name}'")  # the concat demuxer needs the last file again
        (d / "frames.txt").write_text("\n".join(lines) + "\n")
        # The biggest frame sets the size: sharp full-resolution frames (a still page) and smaller ones taken in
        # motion can be mixed, and the small ones are scaled up to match.
        sizes = []
        for f in files:
            with Image.open(f) as im:
                sizes.append(im.size)
        w, h = max(sizes, key=lambda wh: wh[0] * wh[1])
        w, h = w - w % 2, h - h % 2
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = d / "cast.mp4"
        # -reinit_filter 0: one filter graph for every frame size (a new graph per size change cuts the video short)
        ffmpeg(*LOCAL_ONLY, "-reinit_filter", "0", "-f", "concat", "-i", "frames.txt", "-vf",
               f"fps={req.fps},scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
               f"format=yuv420p", "-r", str(req.fps), *ENCODE, "-an", "-t", f"{req.end_s:.4f}",
               "-movflags", "+faststart", "cast.mp4", cwd=d)
        tmp.replace(out)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"path": rel(req.run_id, out), "duration_s": round(duration(out), 3), "frames": len(req.times),
            "size_bytes": out.stat().st_size, "width": w, "height": h}


@app.post("/screencast/assemble", dependencies=[Depends(auth)])
async def cast_assemble(req: CastAssembleReq) -> dict:
    return await asyncio.to_thread(_cast_assemble, req)


# ------------------------------------------------------------------------------------------ local voice (free scaffold)
# The scaffold (animatic) is free: a local voice (Piper, ONNX on CPU) reads the script, and a local speech recogniser
# (faster-whisper) hears where each word landed. Its words come from the script, only the times from what was heard,
# so a misheard word ("Rec" → "wreck") never reaches a caption. Times are good to about a tenth of a second, plenty for
# a preview; the paid voice (ElevenLabs) returns exact timestamps for the final cut.
TTS_VOICE = os.getenv("MEDIA_TTS_VOICE", "/models/piper/en_US-ryan-high.onnx")
ASR_MODEL = os.getenv("MEDIA_ASR_MODEL", "base.en")
FAKE_TTS = os.getenv("MEDIA_FAKE_TTS") == "1"  # tests: a tone per word at known times, no models


class LocalVoice:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._voice = self._asr = None

    def _models(self):
        with self._lock:
            if self._voice is None:
                from faster_whisper import WhisperModel
                from piper import PiperVoice

                self._voice = PiperVoice.load(TTS_VOICE)
                self._asr = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8",
                                         download_root=f"{MODELS_DIR}/whisper")
        return self._voice, self._asr

    def speak(self, text: str, speed: float) -> tuple[np.ndarray, int, list[tuple[str, float, float]]]:
        """(int16 samples, sample rate, heard words with times)."""
        if FAKE_TTS:
            return fake_speech(text)
        from piper import SynthesisConfig

        voice, asr = self._models()
        with self._lock:
            chunks = list(voice.synthesize(text, syn_config=SynthesisConfig(length_scale=1 / speed)))
            sr = chunks[0].sample_rate
            pcm = np.concatenate([np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in chunks])
            audio = pcm.astype(np.float32) / 32768
            a16 = np.interp(np.arange(0, len(audio) * 16000 / sr) * sr / 16000, np.arange(len(audio)), audio)
            segs, _ = asr.transcribe(a16.astype(np.float32), word_timestamps=True, language="en", beam_size=1)
            heard = [(w.word, float(w.start), float(w.end)) for sg in segs for w in sg.words]
        return pcm, sr, heard


def fake_speech(text: str) -> tuple[np.ndarray, int, list[tuple[str, float, float]]]:
    sr, t, heard = 22050, 0.3, []
    for w in text.split():
        heard.append((w, t, t + 0.1 + 0.05 * len(w)))
        t += 0.1 + 0.05 * len(w) + (0.3 if w[-1] in ".!?" else 0.06)
    pcm = np.zeros(int((t + 0.3) * sr), dtype=np.int16)
    for i, (_, a, b) in enumerate(heard):
        n = np.arange(int(a * sr), int(b * sr))
        pcm[n] = (9000 * np.sin(2 * np.pi * (300 + 40 * (i % 8)) * n / sr)).astype(np.int16)
    return pcm, sr, heard


local_voice = LocalVoice()


def align_words(text: str, heard: list[tuple[str, float, float]], total: float) -> dict:
    """An ElevenLabs-style character alignment for `text`, timed from the words that were heard. Matching is done on
    letters and digits only, so different tokenising ("drop-in" vs "drop", "-in") or a misheard word still lines up;
    words that weren't heard at all are placed between their neighbours by length."""
    words = text.split(" ")
    sc, sw = [], []  # the script's letters, and which word each belongs to
    for wi, w in enumerate(words):
        for ch in w:
            if ch.isalnum():
                sc.append(ch.lower())
                sw.append(wi)
    hc, ht = [], []  # what was heard, letter by letter, with times
    for hw, a, b in heard:
        cs = [ch.lower() for ch in hw if ch.isalnum()]
        for k, ch in enumerate(cs):
            hc.append(ch)
            ht.append((a + (b - a) * k / len(cs), a + (b - a) * (k + 1) / len(cs)))
    spans: list[list[float] | None] = [None] * len(words)
    for blk in difflib.SequenceMatcher(None, "".join(sc), "".join(hc), autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            wi, (t0, t1) = sw[blk.a + k], ht[blk.b + k]
            cur = spans[wi]
            spans[wi] = [min(cur[0], t0), max(cur[1], t1)] if cur else [t0, t1]
    i = 0
    while i < len(words):  # fill the gaps
        if spans[i] is not None:
            i += 1
            continue
        j = i
        while j < len(words) and spans[j] is None:
            j += 1
        lo = spans[i - 1][1] if i > 0 else 0.0
        hi = spans[j][0] if j < len(words) else total
        lens = [max(1, len(w)) for w in words[i:j]]
        t = lo
        for k, n in enumerate(lens):
            d = (hi - lo) * n / sum(lens)
            spans[i + k] = [t, t + d]
            t += d
        i = j
    prev = 0.0
    for sp_ in spans:  # never backwards
        sp_[0] = max(sp_[0], prev)
        sp_[1] = max(sp_[1], sp_[0] + 0.02)
        prev = sp_[1]
    chars: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    for wi, w in enumerate(words):
        if wi:
            chars.append(" ")
            starts.append(round(spans[wi - 1][1], 4))
            ends.append(round(spans[wi][0], 4))
        a, b = spans[wi]
        for k, ch in enumerate(w):
            chars.append(ch)
            starts.append(round(a + (b - a) * k / len(w), 4))
            ends.append(round(a + (b - a) * (k + 1) / len(w), 4))
    return {"characters": chars, "character_start_times_seconds": starts, "character_end_times_seconds": ends}


class TTSReq(BaseModel):
    run_id: str
    text: str = Field(min_length=1, max_length=2000)
    out: str
    speed: float = Field(1.0, ge=0.7, le=1.4)


def _tts(req: TTSReq) -> dict:
    out = within(req.run_id, req.out)
    if out.suffix.lower() != ".wav":
        raise HTTPException(400, "out must be a .wav path")
    text = " ".join(req.text.split())
    pcm, sr, heard = local_voice.speak(text, req.speed)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    tmp.replace(out)
    total = len(pcm) / sr
    return {"path": rel(req.run_id, out), "duration_s": round(total, 3), "alignment": align_words(text, heard, total),
            "voice": "fake" if FAKE_TTS else Path(TTS_VOICE).stem, "heard": len(heard)}


@app.post("/tts/local", dependencies=[Depends(auth)])
async def tts_local(req: TTSReq) -> dict:
    return await asyncio.to_thread(_tts, req)


# ------------------------------------------------------------------------------------------ studying references
# Trend analysis looks at what a reference video does, never republishes it: a contact sheet of frames at fixed times
# and a transcript; the downloaded file is deleted once they're made (delete_source).
class FramesReq(BaseModel):
    run_id: str
    src: str
    out: str  # .png
    count: int = Field(8, ge=2, le=16)
    times: list[float] | None = Field(None, min_length=1, max_length=16)  # these moments instead of `count` even ones
    labels: list[str] | None = Field(None, max_length=16)  # one per time, shown under its frame
    inline: bool = False  # also return the sheet as base64, for handing to a model
    delete_source: bool = False


def _frames(req: FramesReq) -> dict:
    src, out = within(req.run_id, req.src), within(req.run_id, req.out)
    if out.suffix.lower() != ".png":
        raise HTTPException(400, "out must be a .png path")
    if not src.is_file():
        raise HTTPException(404, f"no such file: {req.src}")
    try:
        fmt = video_format(src)
        total = duration(src)
        if total <= 0:
            raise HTTPException(400, "the video has no length")
        if req.times:
            times = [round(min(max(float(t), 0.0), max(total - 0.05, 0.0)), 2) for t in req.times]
        else:
            times = [round(total * (k + 0.5) / req.count, 2) for k in range(req.count)]
        labels = [str(x)[:28] for x in (req.labels or [])]
        tiles = []
        font = ImageFont.truetype(FONT, 20)
        with tempfile.TemporaryDirectory(dir=src.parent) as td:
            for k, t in enumerate(times):
                f = Path(td) / f"{k:02d}.png"
                ffmpeg(*LOCAL_ONLY, "-f", fmt, "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-vf",
                       "scale=270:-2", str(f))
                im = Image.open(f).convert("RGB")
                tile = Image.new("RGB", (im.width, im.height + 30), (16, 16, 18))
                tile.paste(im, (0, 0))
                label = f"{t:.1f}s {labels[k]}" if k < len(labels) and labels[k] else f"{t:.1f}s"
                ImageDraw.Draw(tile).text((6, im.height + 5), label, fill="white", font=font)
                tiles.append(tile)
        cols = min(6 if req.times else 8, len(tiles))
        tw, th = max(t.width for t in tiles), max(t.height for t in tiles)
        rows = math.ceil(len(tiles) / cols)
        sheet = Image.new("RGB", (cols * (tw + 6) + 6, rows * (th + 6) + 6), (0, 0, 0))
        for k, tile in enumerate(tiles):
            sheet.paste(tile, (6 + k % cols * (tw + 6), 6 + k // cols * (th + 6)))
        out.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out, "PNG")
    finally:
        if req.delete_source:
            src.unlink(missing_ok=True)
    res = {"sheet": rel(req.run_id, out), "duration_s": round(total, 2), "times": times}
    if req.inline:
        res["png_b64"] = base64.b64encode(out.read_bytes()).decode()
    return res


@app.post("/frames", dependencies=[Depends(auth)])
async def frames(req: FramesReq) -> dict:
    return await asyncio.to_thread(_frames, req)


# A reference's edit, shot by shot: where it cuts (FFmpeg's scene score), and a frame from every shot, so whoever
# studies it sees the shot list (how many cuts, what each shot is, the text on it) rather than evenly spaced moments.
# Cuts are spikes against the local level, as PySceneDetect's adaptive detector does: a hard cut scores high anywhere,
# a jump cut in a talking head scores ~0.15 against a near-zero background, and a fast scroll scores high on every
# frame, so it isn't a string of cuts.
SCENE_THRESHOLD = 0.3  # a hard cut (0.4–1 in practice)
JUMP_THRESHOLD = 0.12  # a jump cut or a big change on a steady shot, if it stands out from the frames around it
SPIKE_RATIO = 3.0  # how far above the local median score a frame must be to count
WINDOW_S = 0.5  # the "frames around it", each side
MIN_SHOT_S = 0.3  # cuts closer than this are one transition (a flash, a whip), not two shots
LONG_SHOT_S = 3.5  # a shot this long gets a frame every SAMPLE_EVERY_S (a scroll, a reveal, text that changes)
SAMPLE_EVERY_S = 2.5
SHOT_TILE_W, SHOTS_PER_SHEET, MAX_SHOT_TILES = 320, 15, 45


class ShotsReq(BaseModel):
    run_id: str
    src: str
    out: str  # .jpg (or .png): the first sheet; more sheets (a fast edit) get -2, -3… before the extension
    threshold: float = Field(SCENE_THRESHOLD, ge=0.1, le=0.9)
    inline: bool = False
    delete_source: bool = False


def scene_scores(src: Path, fmt: str) -> list[tuple[float, float]]:
    """(time, FFmpeg scene-change score) for every frame, on a small copy of the frames."""
    with tempfile.TemporaryDirectory(dir=src.parent) as td:
        log = Path(td) / "scenes.txt"
        ffmpeg(*LOCAL_ONLY, "-f", fmt, "-i", str(src), "-an", "-vf",
               f"scale=192:-2,select='gte(scene,0)',metadata=print:file={log.name}", "-f", "null", "-", cwd=Path(td))
        text = log.read_text() if log.is_file() else ""
    out, t = [], None
    for line in text.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"scene_score=([0-9.]+)", line)
        if m and t is not None:
            out.append((t, float(m.group(1))))
    return out


def find_cuts(scores: list[tuple[float, float]], threshold: float = SCENE_THRESHOLD) -> list[tuple[float, str]]:
    """(time, "cut" | "jump") where the picture changes: a hard cut, or a jump cut / big change on a steady shot. The
    local level is the median score within WINDOW_S each side (a sliding window over the time-ordered scores)."""
    scores = sorted(scores)
    times = [t for t, _ in scores]
    out = []
    for i, (t, sc) in enumerate(scores):
        if sc < JUMP_THRESHOLD:
            continue
        lo, hi = bisect.bisect_left(times, t - WINDOW_S), bisect.bisect_right(times, t + WINDOW_S)
        near = sorted(v for j, (_, v) in enumerate(scores[lo:hi], lo) if j != i)
        level = near[len(near) // 2] if near else 0.0
        if sc >= threshold and (sc >= 0.5 or sc >= SPIKE_RATIO * level):
            out.append((t, "cut"))
        elif sc >= SPIKE_RATIO * max(level, 0.01):
            out.append((t, "jump"))
    return out


def shot_list(cuts: list[tuple[float, str]] | list[float], total: float) -> list[tuple[float, float, str]]:
    """(start, end, how it begins) of every shot: cuts too close together or to either end are merged."""
    edges: list[tuple[float, str]] = [(0.0, "start")]
    for c in sorted((c if isinstance(c, tuple) else (c, "cut")) for c in cuts):
        if c[0] - edges[-1][0] >= MIN_SHOT_S and total - c[0] >= MIN_SHOT_S:
            edges.append(c)
        elif c[1] == "cut" and edges[-1][1] == "jump" and c[0] - edges[-1][0] < MIN_SHOT_S:
            edges[-1] = (edges[-1][0], "cut")
    bounds = [*edges, (total, "end")]
    return [(round(a[0], 2), round(b[0], 2), a[1]) for a, b in zip(bounds, bounds[1:])]


def _shots(req: ShotsReq) -> dict:
    src, out = within(req.run_id, req.src), within(req.run_id, req.out)
    kind = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}.get(out.suffix.lower())
    if kind is None:
        raise HTTPException(400, "out must be a .jpg or .png path")
    if not src.is_file():
        raise HTTPException(404, f"no such file: {req.src}")
    try:
        fmt = video_format(src)
        total = duration(src)
        if total <= 0:
            raise HTTPException(400, "the video has no length")
        shots = shot_list(find_cuts(scene_scores(src, fmt), req.threshold), total)
        # what to show: the very first moment (the hook), then each shot at its middle, or every SAMPLE_EVERY_S of a
        # long one (what changes inside it: a scroll, new text)
        moments: list[tuple[float, str]] = [(min(0.15, total / 2), "#1 start")]
        for n, (a, b, _) in enumerate(shots, 1):
            if b - a >= LONG_SHOT_S:
                k = math.ceil((b - a) / SAMPLE_EVERY_S)
                moments += [(a + (j + 0.5) * (b - a) / k, f"#{n}" if j == 0 else f"#{n} +{(j + 0.5) * (b - a) / k:.0f}s")
                            for j in range(k)]
            else:
                moments.append((a + 0.5 * (b - a), f"#{n}"))
        if len(moments) > MAX_SHOT_TILES:  # a very fast edit: every shot still listed, an even sample shown
            step = len(moments) / MAX_SHOT_TILES
            moments = [moments[int(k * step)] for k in range(MAX_SHOT_TILES)]
        font = ImageFont.truetype(FONT, 22)
        tiles = []
        with tempfile.TemporaryDirectory(dir=src.parent) as td:
            for k, (t, label) in enumerate(moments):
                f = Path(td) / f"{k:02d}.png"
                t = min(max(t, 0.0), max(total - 0.05, 0.0))
                ffmpeg(*LOCAL_ONLY, "-f", fmt, "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-vf",
                       f"scale={SHOT_TILE_W}:-2", str(f))
                im = Image.open(f).convert("RGB")
                tile = Image.new("RGB", (im.width, im.height + 32), (16, 16, 18))
                tile.paste(im, (0, 0))
                ImageDraw.Draw(tile).text((6, im.height + 5), f"{label} · {t:.1f}s", fill="white", font=font)
                tiles.append(tile)
        sheets, b64 = [], []
        out.parent.mkdir(parents=True, exist_ok=True)
        for s, at in enumerate(range(0, len(tiles), SHOTS_PER_SHEET)):
            group = tiles[at:at + SHOTS_PER_SHEET]
            cols = min(5, len(group))
            tw, th = max(t.width for t in group), max(t.height for t in group)
            rows = math.ceil(len(group) / cols)
            sheet = Image.new("RGB", (cols * (tw + 6) + 6, rows * (th + 6) + 6), (0, 0, 0))
            for k, tile in enumerate(group):
                sheet.paste(tile, (6 + k % cols * (tw + 6), 6 + k // cols * (th + 6)))
            path = out if s == 0 else out.with_name(f"{out.stem}-{s + 1}{out.suffix}")
            sheet.save(path, kind, **({"quality": 82} if kind == "JPEG" else {}))
            sheets.append(rel(req.run_id, path))
            if req.inline:
                b64.append(base64.b64encode(path.read_bytes()).decode())
    finally:
        if req.delete_source:
            src.unlink(missing_ok=True)
    lengths = [b - a for a, b, _ in shots]
    res = {"duration_s": round(total, 2),
           "shots": [{"n": n, "start": a, "end": b, **({"jump": True} if how == "jump" else {})}
                     for n, (a, b, how) in enumerate(shots, 1)],
           "cuts": len(shots) - 1, "jump_cuts": sum(1 for *_, how in shots if how == "jump"),
           "avg_shot_s": round(total / len(shots), 2), "longest_shot_s": round(max(lengths), 2), "sheets": sheets}
    if req.inline:
        res["images_b64"] = b64
    return res


@app.post("/shots", dependencies=[Depends(auth)])
async def shots(req: ShotsReq) -> dict:
    return await asyncio.to_thread(_shots, req)


class TranscribeReq(BaseModel):
    run_id: str
    src: str


def _transcribe(req: TranscribeReq) -> dict:
    src = within(req.run_id, req.src)
    if not src.is_file():
        raise HTTPException(404, f"no such file: {req.src}")
    fmt = video_format(src) if PUT_KINDS.get(src.suffix.lower()) == "video" else audio_format(src)
    if FAKE_TTS:
        return {"text": "", "words": []}
    with tempfile.TemporaryDirectory(dir=src.parent) as td:  # 16 kHz mono PCM for the recogniser
        wav = Path(td) / "a.wav"
        ffmpeg(*LOCAL_ONLY, "-f", fmt, "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav))
        with wave.open(str(wav)) as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    _, asr = local_voice._models()
    with local_voice._lock:
        segs, _ = asr.transcribe(a, word_timestamps=True, beam_size=1)
        words = [{"word": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)}
                 for sg in segs for w in sg.words]
    return {"text": " ".join(w["word"] for w in words), "words": words}


@app.post("/transcribe", dependencies=[Depends(auth)])
async def transcribe(req: TranscribeReq) -> dict:
    return await asyncio.to_thread(_transcribe, req)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "image_model": IMAGE_MODEL, "text_model": TEXT_MODEL, "dim": DIM}
