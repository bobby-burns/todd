"""System & harness prompts. Resolution order (first non-empty wins):

1. Dashboard override (stored in the DB, Settings → Prompts)
2. File in the custom prompts dir (./prompts/<name>.md, mounted into the container)
3. Built-in default (todd/prompts/<name>.md)

`harness` is prepended to every agent's prompt. Templates use {{var}} placeholders.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from . import settings

NAMES = ("harness", "planner", "worker", "browser")
DEFAULT_DIR = Path(__file__).parent / "prompts"
CUSTOM_DIR = Path(os.getenv("TODD_PROMPTS_DIR", "/app/custom/prompts"))


def default(name: str) -> str:
    p = DEFAULT_DIR / f"{name}.md"
    return p.read_text() if p.exists() else ""


def custom_file(name: str) -> str:
    p = CUSTOM_DIR / f"{name}.md"
    return p.read_text() if p.exists() else ""


def override(name: str) -> str:
    return (settings.get("prompts") or {}).get(name) or ""


def get(name: str) -> tuple[str, str]:
    """Returns (content, source)."""
    for source, fn in (("dashboard", override), ("file", custom_file), ("default", default)):
        text = fn(name)
        if text.strip():
            return text, source
    return "", "none"


def render(template: str, **vars: object) -> str:
    vars.setdefault("today", date.today().isoformat())
    for k, v in vars.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def compose(name: str, **vars: object) -> str:
    """Full system prompt for an agent: harness + role prompt."""
    harness, _ = get("harness")
    role, _ = get(name)
    return render(f"{harness.strip()}\n\n{role.strip()}", **vars).strip()


def describe() -> dict[str, dict[str, str]]:
    out = {}
    for n in NAMES:
        content, source = get(n)
        out[n] = {"content": content, "source": source, "default": default(n)}
    return out
