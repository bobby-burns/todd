"""Media server. Runs inside the isolated `media` container; only the API calls it, over its own network.

Endpoints (JSON, require X-Media-Token; every path is relative to the run's folder, WORKSPACE_ROOT/<run_id>):
  GET /health                                           -> {ok, image_model, text_model, dim}
  /embed/text     {texts}                               -> {model, image_model, dim, vectors}
  /embed/images   {run_id, items: [{url} | {path, known_sha256?}], save_dir}
                                                        -> {model, dim, results: [{ok, vector, sha256, width, height,
                                                            path, unchanged, error}]}
  /fetch          {run_id, url, save_dir, sha256?}      -> {path, sha256}
  /slides/compose {run_id, width, height, slides: [{image, caption, caption_position, fit}], out_dir, sheet?}
                                                        -> {slides: [path], sheet}
  /render/slideshow {run_id, slides: [path], durations_s, out, fps, motion, music?, music_volume, width, height}
                                                        -> {path, duration_s, size_bytes}
It has no vault access, fetches only from MEDIA_FETCH_HOSTS over https, and reads and writes only inside run folders.
"""

from __future__ import annotations

import asyncio
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
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from pydantic import BaseModel, Field

ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
TOKEN = os.getenv("MEDIA_TOKEN", "")
TOKEN_FILE = os.getenv("MEDIA_TOKEN_FILE", "")  # a random token the API made (read-only here)
IMAGE_MODEL = os.getenv("MEDIA_IMAGE_MODEL", "Qdrant/clip-ViT-B-32-vision")
TEXT_MODEL = os.getenv("MEDIA_TEXT_MODEL", "Qdrant/clip-ViT-B-32-text")
MODELS_DIR = os.getenv("MEDIA_MODELS_DIR", "/models")  # downloaded at build time
FAKE_EMBED = os.getenv("MEDIA_FAKE_EMBED") == "1"  # tests: deterministic vectors, no model
DIM = int(os.getenv("MEDIA_DIM", "512"))
FETCH_HOSTS = {h.strip().lower() for h in os.getenv("MEDIA_FETCH_HOSTS", "images.pexels.com").split(",") if h.strip()}
FETCH_MAX = 20_000_000
FETCH_TIMEOUT = 20
USER_AGENT = "Todd-media/0.1 (+https://github.com/bobby-burns/todd)"  # image hosts turn away default client names
FONT = os.getenv("MEDIA_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
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


async def download(url: str) -> bytes:
    """GET an allow-listed https URL (redirects only to allow-listed hosts), at most FETCH_MAX bytes."""
    _check_url(url)
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT}) as c:
        for _ in range(5):
            try:
                async with asyncio.timeout(FETCH_TIMEOUT):
                    async with c.stream("GET", url) as r:
                        if r.is_redirect:
                            url = urljoin(url, r.headers.get("location", ""))
                            _check_url(url)
                            continue
                        if r.status_code >= 400:
                            raise HTTPException(502, f"fetch failed: HTTP {r.status_code}")
                        if int(r.headers.get("content-length") or 0) > FETCH_MAX:
                            raise HTTPException(413, "image too large")
                        buf = bytearray()
                        async for chunk in r.aiter_bytes():
                            buf += chunk
                            if len(buf) > FETCH_MAX:
                                raise HTTPException(413, "image too large")
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


# ------------------------------------------------------------------------------------------ slides
# Captions stay in TikTok's safe zone: its buttons and caption text cover the bottom and the right edge.
SAFE_TOP, SAFE_BOTTOM = 0.15, 0.70
ANCHORS = {"top": 0.18, "middle": 0.45, "bottom": 0.65}
MAX_LINES = 3


class Slide(BaseModel):
    image: str
    caption: str = ""
    caption_position: Literal["top", "middle", "bottom"] = "middle"
    # cover: fill the slide, cropping the overflow (photos). contain: the whole image, e.g. an app screenshot, on a
    # blurred copy of itself and clear of the caption.
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


def contain(img: Image.Image, w: int, h: int, caption_band: tuple[int, int] | None) -> Image.Image:
    """The whole image, rounded corners, on a blurred and darkened copy of itself, in the largest band of the slide the
    caption leaves free."""
    bg = cover(img, max(1, w // 10), max(1, h // 10)).filter(ImageFilter.GaussianBlur(4))
    slide = ImageEnhance.Brightness(bg.resize((w, h), Image.Resampling.BICUBIC)).enhance(0.45)
    top, bottom = round(0.04 * h), round(0.94 * h)
    if caption_band:
        gap = round(0.025 * h)
        top, bottom = max([(top, caption_band[0] - gap), (caption_band[1] + gap, bottom)], key=lambda b: b[1] - b[0])
    box_w, box_h = round(0.86 * w), max(1, bottom - top)
    scale = min(box_w / img.width, box_h / img.height)
    fg = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
    mask = Image.new("L", fg.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, fg.width - 1, fg.height - 1), round(0.035 * w), fill=255)
    slide.paste(fg, ((w - fg.width) // 2, top + (box_h - fg.height) // 2), mask)
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


def ffmpeg(*args: str, timeout: float = FFMPEG_TIMEOUT) -> None:
    """Run FFmpeg with an argument list (never a shell)."""
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
                           capture_output=True, text=True, timeout=timeout)
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


@app.get("/health")
def health() -> dict:
    return {"ok": True, "image_model": IMAGE_MODEL, "text_model": TEXT_MODEL, "dim": DIM}
