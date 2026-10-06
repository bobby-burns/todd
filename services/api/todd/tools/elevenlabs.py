"""ElevenLabs client: voiceover takes with character timestamps, the master clock of a short (shorts_plan.py).

Only text to speech: Todd doesn't generate music or sound effects. The key (ELEVENLABS_API_KEY) comes from the vault
and never leaves the API: the audio is handed to the media service as bytes."""

from __future__ import annotations

import base64
from typing import Any

import httpx

from ..sdk import ToolError

API = "https://api.elevenlabs.io"
# USD per 1K characters (https://elevenlabs.io/pricing/api, 2026-10-06)
MODELS = {"eleven_multilingual_v2": 0.08, "eleven_v3": 0.08, "eleven_turbo_v2_5": 0.04, "eleven_flash_v2_5": 0.04}
DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_SETTINGS = {"stability": 0.45, "similarity_boost": 0.8, "style": 0.2, "speed": 1.0, "use_speaker_boost": True}
NO_KEY = ("ELEVENLABS_API_KEY isn't in the vault. The human can make one at "
          "https://elevenlabs.io/app/settings/api-keys (the free plan is enough to try it; commercial use needs a paid "
          "plan). Ask for it with "
          "ask_human(..., secret_name=\"ELEVENLABS_API_KEY\").")


def cost_usd(chars: int, model: str) -> float:
    return chars / 1000 * MODELS.get(model, MODELS[DEFAULT_MODEL])


async def _call(method: str, path: str, key: str, timeout: float = 120, **kw: Any) -> httpx.Response:
    try:
        async with httpx.AsyncClient(base_url=API, timeout=timeout) as c:
            r = await c.request(method, path, headers={"xi-api-key": key}, **kw)
    except httpx.HTTPError as e:
        raise ToolError(f"ElevenLabs unreachable: {e}") from e
    if r.status_code < 400:
        return r
    detail = r.text[:400]
    if "quota" in detail.lower():
        raise ToolError("ElevenLabs says the account's character quota is used up: ask the human to top up or upgrade")
    if r.status_code in (401, 403):
        raise ToolError("ElevenLabs refused ELEVENLABS_API_KEY: ask the human to check it in Settings → Vault")
    if r.status_code == 429:
        raise ToolError("ElevenLabs is busy (too many requests at once): try again in a moment")
    raise ToolError(f"ElevenLabs {path} -> {r.status_code}: {detail}")


async def speak(key: str, voice_id: str, text: str, model_id: str = DEFAULT_MODEL,
                settings: dict[str, Any] | None = None, seed: int | None = None) -> dict[str, Any]:
    """One take: {audio (mp3 bytes), alignment {characters, character_start_times_seconds, …}, request_id}."""
    body: dict[str, Any] = {"text": text, "model_id": model_id,
                            "voice_settings": {**DEFAULT_SETTINGS, **(settings or {})}}
    if seed is not None:
        body["seed"] = seed
    r = await _call("POST", f"/v1/text-to-speech/{voice_id}/with-timestamps", key, json=body,
                    params={"output_format": "mp3_44100_128"})
    data = r.json()
    alignment = data.get("alignment") or data.get("normalized_alignment")
    if not data.get("audio_base64") or not alignment:
        raise ToolError("ElevenLabs returned no audio or no timestamps")
    return {"audio": base64.b64decode(data["audio_base64"]), "alignment": alignment,
            "request_id": r.headers.get("request-id")}


async def voices(key: str, search: str | None = None) -> list[dict[str, Any]]:
    """The voices this account can use (premade, library voices it added, its own)."""
    r = await _call("GET", "/v1/voices", key, timeout=30)
    out = []
    for v in r.json().get("voices") or []:
        labels = v.get("labels") or {}
        row = {"voice_id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category"),
               "labels": labels, "description": (v.get("description") or "")[:160]}
        hay = " ".join([str(row["name"]), str(row["description"]), *map(str, labels.values())]).lower()
        if not search or all(t in hay for t in search.lower().split()):
            out.append(row)
    return out
