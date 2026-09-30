"""Client for the media container (image embeddings and captioned slides for the video toolset). It mounts the
workspace, so every path here is relative to the run's folder."""

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
