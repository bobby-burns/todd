"""The run's workspace, read-only, for the dashboard's Files view: its folder in the sandbox (`/workspace/<run_id>`,
shared by every agent in the run).

What the human sees is what the agents made, with the same care as the rest of the UI: vault values are masked
wherever they appear, `.env` files show their names but not their values (unless the human asks), and private key
files aren't shown at all.
"""

from __future__ import annotations

import base64
import posixpath
import re
from typing import Any

from . import vault
from .config import config
from .sdk import ToolError
from .tools import sandbox

KEY_FILE = re.compile(r"(\.(pem|p8|p12|pfx|key|keystore|jks|mobileprovision)$|(^|/)id_(rsa|ed25519|ecdsa|dsa)$)",
                      re.IGNORECASE)
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
ENV_FILE = re.compile(r"(^|/)\.env(\.[\w.-]+)?$|(^|/)[\w.-]*\.env$", re.IGNORECASE)
ENV_LINE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.-]*\s*[=:]\s*)(\S.*)$", re.MULTILINE)
MASK = "••••••••"


def root(run_id: str) -> str:
    return f"{config.workspace_root}/{run_id}"


def clean(path: str) -> str:
    """A path relative to the run's folder, or ToolError if it points outside it."""
    p = posixpath.normpath("/" + (path or "").strip()).lstrip("/")
    if p in ("", "."):
        raise ToolError("pick a file")
    if p.startswith(".."):
        raise ToolError("path outside this run's folder")
    return p


async def tree(run_id: str) -> dict[str, Any]:
    """Every file and folder the agents left in the run's folder."""
    return await sandbox.call("/files/tree", {"base": root(run_id)}, timeout=30)


async def view(run_id: str, path: str, reveal: bool = False) -> dict[str, Any]:
    """One file, ready to show: text (vault values masked), an image, or a note when it can't be shown."""
    p = clean(path)
    if KEY_FILE.search(p):
        return {"path": p, "kind": "hidden", "note": "A private key file. Todd doesn't show these."}
    r = await sandbox.call("/files/view", {"base": root(run_id), "path": p}, timeout=30)
    out: dict[str, Any] = {"path": p, **r}
    if r.get("kind") != "text":
        return out
    text = r.get("content") or ""
    if PRIVATE_KEY.search(text):
        return {"path": p, "kind": "hidden", "size": r.get("size"), "mtime": r.get("mtime"),
                "note": "This file holds a private key. Todd doesn't show these."}
    text = vault.scrub(text)
    if ENV_FILE.search(p):
        out["env"] = True
        if not reveal:
            text = ENV_LINE.sub(lambda m: m.group(1) + MASK, text)
    out["content"] = text
    return out


async def download(run_id: str, path: str) -> tuple[str, bytes]:
    """(file name, bytes) for a download. Text files get the same masking as the viewer (vault values)."""
    p = clean(path)
    if KEY_FILE.search(p):
        raise ToolError("private key files can't be downloaded here")
    r = await sandbox.call("/files/view", {"base": root(run_id), "path": p, "raw": True}, timeout=120)
    data = base64.b64decode(r.get("data") or "")
    if b"\0" not in data[:8192]:
        text = data.decode(errors="replace")
        if PRIVATE_KEY.search(text):
            raise ToolError("this file holds a private key and can't be downloaded here")
        data = vault.scrub(text).encode()
    return posixpath.basename(p), data
