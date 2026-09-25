"""Sandbox exec server. Runs inside the isolated `sandbox` container; the API calls it over the internal network.

Endpoints (all POST, JSON, require X-Sandbox-Token):
  /exec        {cmd, cwd, timeout, env}  -> {exit_code, output, timed_out, duration_s}
  /files/write {path, content}           -> {ok}
  /files/read  {path}                    -> {content}
  /files/list  {path, depth}             -> {entries}
All paths are confined to WORKSPACE_ROOT.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
TOKEN = os.getenv("SANDBOX_TOKEN", "change-me-sandbox-token")
MAX_OUTPUT = int(os.getenv("SANDBOX_MAX_OUTPUT", "20000"))
SKIP = {"node_modules", ".git", ".next", "dist", "build", ".turbo", ".venv", "__pycache__", ".vercel"}

app = FastAPI(title="todd-sandbox")
ROOT.mkdir(parents=True, exist_ok=True)


def auth(x_sandbox_token: str = Header(default="")) -> None:
    if x_sandbox_token != TOKEN:
        raise HTTPException(401, "bad sandbox token")


def safe(path: str) -> Path:
    p = (Path(path) if path.startswith("/") else ROOT / path).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise HTTPException(400, "path outside workspace")
    return p


class ExecReq(BaseModel):
    cmd: str
    cwd: str = str(ROOT)
    timeout: int = 300
    env: dict[str, str] = {}


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return text[:half] + f"\n…[{len(text) - MAX_OUTPUT} chars truncated]…\n" + text[-half:]


@app.post("/exec", dependencies=[Depends(auth)])
async def exec_(req: ExecReq) -> dict:
    cwd = safe(req.cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CI": "1", "TERM": "dumb", "NO_COLOR": "1", "npm_config_yes": "true", **req.env}
    env.pop("SANDBOX_TOKEN", None)
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", req.cmd, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=max(1, req.timeout))
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:  # a daemonized grandchild can keep the pipe open; don't wait on it forever
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            out = b"[process killed after timeout; output unavailable]"
    text = out.decode(errors="replace")
    for v in req.env.values():  # never echo injected secrets back
        if len(v) >= 8:
            text = text.replace(v, "***")
    return {"exit_code": proc.returncode, "output": _clip(text), "timed_out": timed_out,
            "duration_s": round(time.monotonic() - start, 2)}


class WriteReq(BaseModel):
    path: str
    content: str


@app.post("/files/write", dependencies=[Depends(auth)])
def write(req: WriteReq) -> dict:
    p = safe(req.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(req.content)
    return {"ok": True, "path": str(p)}


class PathReq(BaseModel):
    path: str
    depth: int = 2


@app.post("/files/read", dependencies=[Depends(auth)])
def read(req: PathReq) -> dict:
    p = safe(req.path)
    if not p.is_file():
        raise HTTPException(404, f"no such file: {req.path}")
    data = p.read_bytes()
    if b"\0" in data[:4096]:
        return {"content": f"<binary file, {len(data)} bytes>"}
    return {"content": _clip(data.decode(errors="replace"))}


@app.post("/files/list", dependencies=[Depends(auth)])
def listdir(req: PathReq) -> dict:
    base = safe(req.path)
    if not base.is_dir():
        raise HTTPException(404, f"no such directory: {req.path}")
    entries: list[str] = []

    def walk(d: Path, depth: int) -> None:
        for child in sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name)):
            if child.name in SKIP:
                entries.append(str(child.relative_to(base)) + "/ (skipped)")
                continue
            rel = str(child.relative_to(base))
            entries.append(rel + ("/" if child.is_dir() else f"  ({child.stat().st_size} B)"))
            if child.is_dir() and depth > 1 and len(entries) < 800:
                walk(child, depth - 1)

    walk(base, max(1, req.depth))
    return {"entries": entries[:800], "truncated": len(entries) > 800}


@app.get("/health")
def health() -> dict:
    return {"ok": True}
