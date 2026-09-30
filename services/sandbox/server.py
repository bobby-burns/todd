"""Sandbox exec server. Runs inside the isolated `sandbox` container; the API calls it over the internal network.

Endpoints (all POST, JSON, require X-Sandbox-Token):
  /exec        {cmd, cwd, timeout, env}  -> {exit_code, output, timed_out, duration_s}
  /files/write {path, content}           -> {ok}
  /files/read  {path}                    -> {content}
  /files/list  {path, depth}             -> {entries}
  /files/tree  {base}                    -> {exists, entries: [{path, dir, size, mtime, …}], truncated}
  /files/view  {base, path, raw}         -> {kind: text|image|binary|raw, size, mtime, content | data}
All paths are confined to WORKSPACE_ROOT (tree and view to `base`, one run's folder).
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
TOKEN = os.getenv("SANDBOX_TOKEN", "change-me-sandbox-token")
TOKEN_FILE = os.getenv("SANDBOX_TOKEN_FILE", "")  # the signed-in runner: a random token the API made (read-only here)
MAX_OUTPUT = int(os.getenv("SANDBOX_MAX_OUTPUT", "20000"))
SKIP = {"node_modules", ".git", ".next", "dist", "build", ".turbo", ".venv", "__pycache__", ".vercel"}

# Previews: 0.0.0.0:BASE+i -> 127.0.0.1:PORT, so the agents' browser (through the API's relay) can open a dev server
# running here even if it only listens on localhost. The same list and base as the API and the browser container.
PREVIEW_PORTS = [int(p) for p in os.getenv("PREVIEW_PORTS", "3000,3001,4173,4321,5000,5173,8000,8080,8081,19006")
                 .replace(" ", "").split(",") if p.isdigit()]
PREVIEW_BASE = int(os.getenv("PREVIEW_RELAY_BASE", "17000"))
EXEC_PORT = 7000  # this server: never exposed as a preview


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        pass


async def _relay(listen_port: int, target_port: int):
    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            tr, tw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", target_port), 5)
        except (OSError, asyncio.TimeoutError):
            w.close()
            return
        try:
            await asyncio.gather(_pipe(r, tw), _pipe(tr, w))
        finally:
            for x in (w, tw):
                try:
                    x.close()
                except Exception:  # noqa: BLE001
                    pass

    return await asyncio.start_server(handle, "0.0.0.0", listen_port)


@asynccontextmanager
async def lifespan(_: FastAPI):
    servers = []
    for i, port in enumerate(PREVIEW_PORTS if os.getenv("SANDBOX_PREVIEWS", "1") != "0" else []):
        if port == EXEC_PORT:
            continue
        try:
            servers.append(await _relay(PREVIEW_BASE + i, port))
        except OSError as e:
            print(f"preview relay for port {port} not started: {e}", flush=True)
    yield
    for s in servers:
        s.close()


app = FastAPI(title="todd-sandbox", lifespan=lifespan)
ROOT.mkdir(parents=True, exist_ok=True)


def _token() -> str:
    if TOKEN_FILE:
        try:
            return Path(TOKEN_FILE).read_text().strip()
        except OSError:
            return ""  # not made yet: refuse everything until it is
    return TOKEN


def auth(x_sandbox_token: str = Header(default="")) -> None:
    import hmac

    token = _token()
    if not token or not hmac.compare_digest(x_sandbox_token.encode(), token.encode()):
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


# The event loop's subprocess pipes leak extra copies of the output socket into the child (fds above 2). Anything the
# command leaves running in the background (a dev server, a CLI login) would inherit them and hold this request open
# until the timeout, so every command first closes the fds it doesn't need.
CLOSE_FDS = ('for _fd in /proc/$$/fd/*; do _fd=${_fd##*/}; case $_fd in 0|1|2) ;; '
             '*) eval "exec $_fd>&-" 2>/dev/null;; esac; done; unset _fd\n')


def _kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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
    env.pop("SANDBOX_TOKEN_FILE", None)
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", CLOSE_FDS + req.cmd, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
    )
    chunks: list[bytes] = []

    async def read() -> None:  # buffered as it arrives, so a timeout keeps what the command printed
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(65536):
            chunks.append(chunk)

    reader = asyncio.create_task(read())
    timed_out = held = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=max(1, req.timeout))
    except asyncio.TimeoutError:
        timed_out = True
        _kill(proc.pid)
    try:  # the output ends once nothing is writing to it
        await asyncio.wait_for(asyncio.shield(reader), timeout=5 if timed_out else 3)
    except asyncio.TimeoutError:
        held = not timed_out  # a background process started without redirecting its output
        _kill(proc.pid)
        try:
            await asyncio.wait_for(asyncio.shield(reader), timeout=2)
        except asyncio.TimeoutError:
            reader.cancel()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    text = b"".join(chunks).decode(errors="replace")
    if timed_out:
        text += f"\n[stopped after the {req.timeout}s timeout]"
    elif held:
        text += ("\n[stopped background processes still writing to this command's output; start long-running ones "
                 "with `nohup CMD > FILE 2>&1 &`]")
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


# ------------------------------------------------------------------ read-only browsing (the dashboard's Files view)
# Confined to one run's folder (`base`): a path that resolves outside it, e.g. through a symlink, is refused.
TREE_MAX = 5000
VIEW_MAX = 400_000  # characters of text shown at once
RAW_MAX = 25_000_000  # bytes for images and downloads
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
          ".webp": "image/webp", ".avif": "image/avif", ".ico": "image/x-icon", ".bmp": "image/bmp",
          ".svg": "image/svg+xml"}


def _within(base: str, path: str = "") -> tuple[Path, Path]:
    root = safe(base)
    p = (root / path.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "path outside this run's folder")
    return root, p


class TreeReq(BaseModel):
    base: str


@app.post("/files/tree", dependencies=[Depends(auth)])
def tree(req: TreeReq) -> dict:
    """Every file and folder under `base` (dependency and build folders are listed but not opened)."""
    root, _ = _within(req.base)
    if not root.is_dir():
        return {"exists": False, "entries": [], "truncated": False}
    entries: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):  # doesn't follow symlinked folders
        here = Path(dirpath)
        dirnames.sort()
        for name in list(dirnames):
            p = here / name
            skipped = name in SKIP or p.is_symlink()
            entries.append({"path": str(p.relative_to(root)), "dir": True, "skipped": skipped,
                            "mtime": _mtime(p)})
            if skipped:
                dirnames.remove(name)
        for name in sorted(filenames):
            p = here / name
            try:
                st = p.lstat()
            except OSError:
                continue
            entries.append({"path": str(p.relative_to(root)), "dir": False, "size": st.st_size,
                            "mtime": st.st_mtime, "link": p.is_symlink()})
        if len(entries) >= TREE_MAX:
            return {"exists": True, "entries": entries[:TREE_MAX], "truncated": True}
    return {"exists": True, "entries": entries, "truncated": False}


def _mtime(p: Path) -> float | None:
    try:
        return p.lstat().st_mtime
    except OSError:
        return None


class ViewReq(BaseModel):
    base: str
    path: str
    raw: bool = False  # the whole file as base64 (downloads)


@app.post("/files/view", dependencies=[Depends(auth)])
def view(req: ViewReq) -> dict:
    """One file: text (up to VIEW_MAX characters), an image, or just its size when it's binary."""
    import base64

    _, p = _within(req.base, req.path)
    if not p.is_file():
        raise HTTPException(404, f"no such file: {req.path}")
    st = p.stat()
    out: dict = {"size": st.st_size, "mtime": st.st_mtime}
    if req.raw:
        if st.st_size > RAW_MAX:
            raise HTTPException(413, "file too large to download here")
        return {**out, "kind": "raw", "data": base64.b64encode(p.read_bytes()).decode()}
    mime = IMAGES.get(p.suffix.lower())
    if mime:
        if st.st_size > RAW_MAX:
            return {**out, "kind": "binary"}
        return {**out, "kind": "image", "mime": mime, "data": base64.b64encode(p.read_bytes()).decode()}
    with p.open("rb") as f:
        head = f.read(VIEW_MAX * 4 + 4)
    if b"\0" in head[:8192]:
        return {**out, "kind": "binary"}
    text = head.decode(errors="replace")
    truncated = len(text) > VIEW_MAX or st.st_size > len(head)
    return {**out, "kind": "text", "content": text[:VIEW_MAX], "truncated": truncated}


@app.get("/health")
def health() -> dict:
    return {"ok": True}
