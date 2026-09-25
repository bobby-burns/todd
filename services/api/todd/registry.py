"""Toolsets: the building blocks the planner hands to the agents it spawns.

  * built-in toolsets (sandbox, browser, vercel, github, web, vault, accounts)
  * plugin toolsets: every LangChain tool found in ./plugins/*.py (module-level BaseTool instances or a `TOOLS`
    list). A tool's toolset is `todd_tool(toolset=...)`, defaulting to the plugin file's name.
  * MCP toolsets: one per MCP server configured in Settings ("mcp_<server>").

The planner itself only orchestrates: it gets the orchestration tools, human tools, and any tool marked
`planner=True` (check_accounts, fetch_url, vault_list, …).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from . import settings
from .accounts import ACCOUNT_TOOLS
from .agents.dynamic import ORCHESTRATION_TOOLS
from .integrations import find_integrations
from .sdk import SOURCE_KEY, for_planner, tag, toolset_of
from .tools.browser_tools import BROWSER_TOOLS
from .tools.human import HUMAN_TOOLS
from .tools.infra import GITHUB_TOOLS, VAULT_TOOLS, VERCEL_TOOLS, resolve_secrets
from .tools.sandbox_tools import SANDBOX_TOOLS
from .tools.web import WEB_TOOLS

log = logging.getLogger("todd.registry")
PLUGINS_DIR = Path(os.getenv("TODD_PLUGINS_DIR", "/app/custom/plugins"))


@dataclass
class Toolset:
    name: str
    description: str
    tools: list[BaseTool] = field(default_factory=list)
    source: str = "builtin"
    guide: str = ""  # usage tips added to the system prompt of agents that get this toolset

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "source": self.source,
                "tools": [t.name for t in self.tools]}


BUILTIN_TOOLSETS: dict[str, Toolset] = {
    "sandbox": Toolset("sandbox", "Linux sandbox (node 22, pnpm, git, python, vercel/firebase CLIs) with a workspace "
                       "shared by all agents in this run: shell, read/write/list files, git_push to GitHub.",
                       SANDBOX_TOOLS,
                       guide="Commands have no TTY: always pass non-interactive flags (e.g. `npx create-next-app@latest "
                             "app --ts --tailwind --eslint --app --use-npm --yes`). Verify with a real build before "
                             "pushing. Config goes in env vars with a `.env.example`; never commit `.env*`. Paths are "
                             "relative to the run workspace, which other agents may also use: stay in your directory."),
    "browser": Toolset("browser", "A real Chromium where the human is signed in to their accounts: browse(task) "
                       "runs multi-step web tasks (consoles without APIs, forms, sign-in-gated pages, card "
                       "checkout with approval). One browser, shared: browser tasks run one at a time.",
                       BROWSER_TOOLS,
                       guide="Last resort: call find_integrations first and pass why_not_api. Each browse() call should be one focused goal with a clear done condition and the exact "
                             "values to report. Don't create new accounts; the human is usually signed in already. "
                             "Copy keys/config values exactly into your summary (or vault_store them if you have vault)."),
    "vercel": Toolset("vercel", "Vercel API: check/buy domains (spend-gated), projects, domains/DNS, env vars, "
                      "deployments.", VERCEL_TOOLS),
    "github": Toolset("github", "GitHub API: create repositories.", GITHUB_TOOLS),
    "web": Toolset("web", "api_request: call any REST/GraphQL API with vault secrets as {{secret:NAME}}; fetch_url: "
                   "read docs and public pages. The default way to work with services that have an API.", WEB_TOOLS,
                   guide="Look up the right API with find_integrations, read its docs with fetch_url, then call it with "
                         "api_request. Check status codes and report IDs/URLs from responses."),
    "vault": Toolset("vault", "List secret names and store new secrets (referenced as {{secret:NAME}}).",
                     VAULT_TOOLS),
    "accounts": Toolset("accounts", "See which services the browser is signed in to; ask the human to sign in.",
                        ACCOUNT_TOOLS),
}
# Under the Claude Code engine the agent drives the browser itself (every model call goes through the human's plan).
from .tools.browser_direct import DIRECT_BROWSER_TOOLS  # noqa: E402

DIRECT_BROWSER_TOOLSET = Toolset(
    "browser", "A real Chromium where the human is signed in to their accounts, driven step by step: browser_start, "
    "then browser_navigate/click/type/keys/scroll/search, browser_done. For consoles without APIs, forms, "
    "sign-in-gated pages and card checkout with approval. One browser, shared: agents take turns.",
    DIRECT_BROWSER_TOOLS,
    guide="Last resort: call find_integrations first and pass why_not_api to browser_start. Read the page state "
          "after each action and use element [index] numbers. Don't create new accounts; the human is usually signed "
          "in already. On captchas/2FA, ask_human (they can take over the live browser). Always call browser_done "
          "when finished. Copy keys/config values exactly into your summary (or vault_store them if you have vault).")

for _ts in [*BUILTIN_TOOLSETS.values(), DIRECT_BROWSER_TOOLSET]:
    for _t in _ts.tools:
        tag(_t, source="builtin")
for _t in [*ORCHESTRATION_TOOLS, *HUMAN_TOOLS, find_integrations]:
    tag(_t, source="builtin", planner=True)

_plugin_toolsets: dict[str, Toolset] = {}
_plugin_errors: list[dict[str, str]] = []


def _builtin_tool_names() -> set[str]:
    names = {t.name for ts in [*BUILTIN_TOOLSETS.values(), DIRECT_BROWSER_TOOLSET] for t in ts.tools}
    return names | {t.name for t in [*ORCHESTRATION_TOOLS, *HUMAN_TOOLS, find_integrations]} | {"finish"}


def load_plugins() -> None:
    """(Re)load every plugin file. Safe to call at runtime from the dashboard."""
    global _plugin_toolsets, _plugin_errors
    toolsets: dict[str, Toolset] = {}
    errors: list[dict[str, str]] = []
    reserved = _builtin_tool_names()
    if PLUGINS_DIR.is_dir():
        for path in sorted(PLUGINS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            mod_name = f"todd_plugin_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                found = list(getattr(mod, "TOOLS", []) or [])
                found += [v for v in vars(mod).values() if isinstance(v, BaseTool) and v not in found]
                desc = (mod.__doc__ or "").strip().split("\n")[0] or f"Tools from plugins/{path.name}"
                for t in found:
                    tag(t, source=f"plugin:{path.name}")
                    if t.name in reserved:
                        errors.append({"file": path.name, "error": f"tool {t.name!r} clashes with a built-in; skipped"})
                        continue
                    ts_name = toolset_of(t) or path.stem
                    if ts_name in BUILTIN_TOOLSETS:
                        errors.append({"file": path.name,
                                       "error": f"toolset {ts_name!r} is built-in; use another name ({t.name} skipped)"})
                        continue
                    tag(t, toolset=ts_name)
                    ts = toolsets.setdefault(ts_name, Toolset(ts_name, desc, [], f"plugin:{path.name}"))
                    ts.tools.append(t)
            except Exception:
                errors.append({"file": path.name, "error": traceback.format_exc(limit=3)})
                log.exception("failed to load plugin %s", path)
    _plugin_toolsets, _plugin_errors = toolsets, errors


async def load_mcp_toolsets() -> tuple[dict[str, Toolset], list[dict[str, str]]]:
    """One toolset per MCP server configured in Settings (langchain-mcp-adapters). Returns (toolsets, errors)."""
    servers: dict[str, Any] = settings.get("mcp_servers") or {}
    if not servers:
        return {}, []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    out: dict[str, Toolset] = {}
    errors: list[dict[str, str]] = []
    for name, conn in servers.items():
        try:
            desc = conn.get("description") if isinstance(conn, dict) else None
            client = MultiServerMCPClient({name: _resolve(conn)}, tool_name_prefix=True)
            tools = await client.get_tools()
            ts_name = f"mcp_{name}"
            for t in tools:
                tag(t, toolset=ts_name, source=f"mcp:{name}")
            out[ts_name] = Toolset(ts_name, desc or f"MCP server '{name}': {', '.join(t.name for t in tools[:12])}",
                                   list(tools), f"mcp:{name}")
        except Exception as e:  # noqa: BLE001
            errors.append({"server": name, "error": _root_cause(e)})
    return out, errors


def _root_cause(e: BaseException) -> str:
    """MCP clients raise ExceptionGroups from their task groups; report the innermost real error."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    msg = str(e) or type(e).__name__
    return f"{type(e).__name__}: {msg}"[:300]


def _resolve(value: Any) -> Any:
    if isinstance(value, str):
        return resolve_secrets(value, allow_protected=True)  # MCP config is written by the human
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items() if k not in ("agents", "description")}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


def all_toolsets(extra: dict[str, Toolset] | None = None, engine: str = "api") -> dict[str, Toolset]:
    builtin = dict(BUILTIN_TOOLSETS)
    if engine == "claude_code":
        builtin["browser"] = DIRECT_BROWSER_TOOLSET
    return {**builtin, **_plugin_toolsets, **(extra or {})}


def planner_tools(toolsets: dict[str, Toolset]) -> list[BaseTool]:
    out, seen = [], set()
    for t in [*ORCHESTRATION_TOOLS, *HUMAN_TOOLS, find_integrations,
              *(t for ts in toolsets.values() for t in ts.tools if for_planner(t))]:
        if t.name not in seen:
            out.append(t)
            seen.add(t.name)
    return out


def toolsets_prompt(toolsets: dict[str, Toolset]) -> str:
    return "\n".join(f"- `{ts.name}` — {ts.description}" for ts in toolsets.values())


def catalog(extra: dict[str, Toolset] | None = None) -> dict[str, Any]:
    from . import settings

    tsets = all_toolsets(extra, engine=settings.get("engine") or "claude_code")
    tools = []
    for t in planner_tools(tsets)[: len(ORCHESTRATION_TOOLS) + len(HUMAN_TOOLS) + 1]:
        tools.append({"name": t.name, "description": (t.description or "").split("\n")[0][:300],
                      "toolset": "planner", "planner": True, "source": "builtin"})
    for ts in tsets.values():
        for t in ts.tools:
            tools.append({"name": t.name, "description": (t.description or "").split("\n")[0][:300],
                          "toolset": ts.name, "planner": for_planner(t),
                          "source": (t.metadata or {}).get(SOURCE_KEY, ts.source)})
    return {"toolsets": [ts.info() for ts in tsets.values()], "tools": tools, "plugin_errors": _plugin_errors,
            "plugins_dir": str(PLUGINS_DIR)}
