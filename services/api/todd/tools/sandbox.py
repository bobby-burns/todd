"""Client for the sandbox container (an isolated box with node/git/python where code agents work)."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import config
from ..sdk import ToolError


async def call(path: str, payload: dict[str, Any], timeout: float = 120, signed_in: bool = False) -> dict[str, Any]:
    url, token, name = config.sandbox_url, config.sandbox_token, "sandbox"
    if signed_in and config.signedin_url:
        url, token, name = config.signedin_url, config.signedin_token, "signed-in runner"
    try:
        async with httpx.AsyncClient(base_url=url, timeout=timeout + 15) as c:
            r = await c.post(path, json=payload, headers={"X-Sandbox-Token": token})
    except httpx.HTTPError as e:
        raise ToolError(f"{name} unreachable at {url}: {e}") from e
    if r.status_code >= 400:
        raise ToolError(f"{name} {path} -> {r.status_code}: {r.text[:1000]}")
    return r.json()


async def exec_(cmd: str, cwd: str, timeout: int = 300, env: dict[str, str] | None = None,
                signed_in: bool = False) -> dict[str, Any]:
    """Run a command. signed_in: it uses one of your sign-ins (a token in `env`, a CLI's sign-in files), so it runs
    in the signed-in runner (same workspace, its own container) when there is one."""
    return await call("/exec", {"cmd": cmd, "cwd": cwd, "timeout": timeout, "env": env or {}}, timeout=timeout,
                      signed_in=signed_in)


async def write(path: str, content: str) -> dict[str, Any]:
    return await call("/files/write", {"path": path, "content": content})


async def read(path: str) -> dict[str, Any]:
    return await call("/files/read", {"path": path})


async def listdir(path: str, depth: int = 2) -> dict[str, Any]:
    return await call("/files/list", {"path": path, "depth": depth})
