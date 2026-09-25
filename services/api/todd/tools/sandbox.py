"""Client for the sandbox container (an isolated box with node/git/python where code agents work)."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import config
from ..sdk import ToolError


async def call(path: str, payload: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=config.sandbox_url, timeout=timeout + 15) as c:
            r = await c.post(path, json=payload, headers={"X-Sandbox-Token": config.sandbox_token})
    except httpx.HTTPError as e:
        raise ToolError(f"sandbox unreachable at {config.sandbox_url}: {e}") from e
    if r.status_code >= 400:
        raise ToolError(f"sandbox {path} -> {r.status_code}: {r.text[:1000]}")
    return r.json()


async def exec_(cmd: str, cwd: str, timeout: int = 300, env: dict[str, str] | None = None) -> dict[str, Any]:
    return await call("/exec", {"cmd": cmd, "cwd": cwd, "timeout": timeout, "env": env or {}}, timeout=timeout)


async def write(path: str, content: str) -> dict[str, Any]:
    return await call("/files/write", {"path": path, "content": content})


async def read(path: str) -> dict[str, Any]:
    return await call("/files/read", {"path": path})


async def listdir(path: str, depth: int = 2) -> dict[str, Any]:
    return await call("/files/list", {"path": path, "depth": depth})
