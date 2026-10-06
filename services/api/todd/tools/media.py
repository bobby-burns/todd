"""Client for the media container (image embeddings, captioned slides and MP4 slideshows for the video toolset). It
mounts the workspace, so every path here is relative to the run's folder."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import config
from ..sdk import ToolError


async def call(path: str, payload: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
    url = config.media_url
    try:
        async with httpx.AsyncClient(base_url=url, timeout=timeout + 15) as c:
            r = await c.post(path, json=payload, headers={"X-Media-Token": config.media_token})
    except httpx.HTTPError as e:
        raise ToolError(f"media service unreachable at {url}: {e}") from e
    if r.status_code >= 400:
        raise ToolError(f"media {path} -> {r.status_code}: {r.text[:1000]}")
    return r.json()


async def embed_text(texts: list[str]) -> dict[str, Any]:
    """{model, dim, vectors}: one L2-normalized vector per text."""
    return await call("/embed/text", {"texts": texts}, timeout=60)


async def embed_images(run_id: str, items: list[dict[str, Any]], save_dir: str = "video/_cache") -> dict[str, Any]:
    """items: {url} (downloaded to save_dir/<sha256>.<ext>) or {path, known_sha256?} (read in place; not embedded
    again while it still hashes to known_sha256). Returns {model, dim, results: [{ok, vector, sha256, …}]}."""
    return await call("/embed/images", {"run_id": run_id, "items": items, "save_dir": save_dir}, timeout=300)


async def fetch(run_id: str, url: str, save_dir: str = "video/_cache", sha256: str | None = None) -> dict[str, Any]:
    """Download only (no embedding): {path, sha256}. Nothing is downloaded when save_dir/<sha256>.* is there."""
    return await call("/fetch", {"run_id": run_id, "url": url, "save_dir": save_dir, "sha256": sha256}, timeout=60)


async def compose(run_id: str, slides: list[dict[str, Any]], out_dir: str, width: int = 1080, height: int = 1920,
                  sheet: str | None = None) -> dict[str, Any]:
    """Captioned slides as out_dir/01.png, 02.png, … (and all of them side by side in `sheet`)."""
    return await call("/slides/compose", {"run_id": run_id, "width": width, "height": height, "slides": slides,
                                          "out_dir": out_dir, "sheet": sheet}, timeout=300)


async def render(run_id: str, slides: list[str], durations_s: list[float], out: str, fps: int = 30,
                 motion: str = "kenburns", music: str | None = None, music_volume: float = 0.8, width: int = 1080,
                 height: int = 1920) -> dict[str, Any]:
    """The slides as one MP4 (H.264, `fps`, a slow zoom per slide unless motion is "none", the music trimmed to fit):
    {path, duration_s, size_bytes}."""
    return await call("/render/slideshow", {"run_id": run_id, "slides": slides, "durations_s": durations_s, "out": out,
                                            "fps": fps, "motion": motion, "music": music,
                                            "music_volume": music_volume, "width": width, "height": height},
                      timeout=600)


async def put(run_id: str, path: str, data: bytes) -> dict[str, Any]:
    """Store bytes (audio, image or video only) at `path` in the run folder: {path, size_bytes, sha256}."""
    import base64

    return await call("/files/put", {"run_id": run_id, "path": path, "data_b64": base64.b64encode(data).decode()},
                      timeout=120)


async def probe(run_id: str, paths: list[str]) -> dict[str, dict[str, Any]]:
    """{path: {kind, duration_s, width, height, fps}} for the files that could be read."""
    if not paths:
        return {}
    r = await call("/probe", {"run_id": run_id, "paths": paths[:40]}, timeout=120)
    return {i["path"]: i for i in r.get("items") or [] if i.get("ok")}


async def render_timeline(run_id: str, out: str, timeline: dict[str, Any]) -> dict[str, Any]:
    """Render a shorts timing plan (shorts_plan.plan) to an MP4: {path, duration_s, frames, size_bytes}."""
    body = {k: timeline[k] for k in ("fps", "width", "height", "video", "captions", "overlays") if k in timeline}
    if timeline.get("voice"):
        body["voice"] = timeline["voice"]
    if timeline.get("music"):
        body["music"] = timeline["music"]
    return await call("/render/timeline", {"run_id": run_id, "out": out, **body}, timeout=900)


async def tts_local(run_id: str, text: str, out: str, speed: float = 1.0) -> dict[str, Any]:
    """The free scaffold voice (Piper, timed by a local recogniser): {path, duration_s, alignment, voice}."""
    return await call("/tts/local", {"run_id": run_id, "text": text, "out": out, "speed": speed}, timeout=300)


async def cast_upload(run_id: str, session: str, frames: list[bytes], chunk: int = 40) -> None:
    """Hand a browser recording's JPEG frames to the media service, a chunk at a time."""
    import base64

    for k in range(0, len(frames), chunk):
        batch = [{"i": k + j, "data_b64": base64.b64encode(f).decode()} for j, f in enumerate(frames[k:k + chunk])]
        await call("/screencast/put", {"run_id": run_id, "session": session, "frames": batch}, timeout=120)


async def cast_assemble(run_id: str, session: str, times: list[float], end_s: float, out: str) -> dict[str, Any]:
    """The uploaded frames as a 30 fps MP4, each frame held until the next one's time."""
    return await call("/screencast/assemble", {"run_id": run_id, "session": session, "times": times,
                                               "end_s": end_s, "out": out}, timeout=600)
