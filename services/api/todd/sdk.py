"""Todd tool SDK — the public surface for custom tools (built-in tools use it too).

Tools are plain LangChain tools, so anything from the LangChain ecosystem works. Todd adds:

  * todd_tool(...)   -> @tool plus routing: which *toolset* the tool belongs to, and whether the planner gets it
  * get_ctx()        -> the current RunContext (emit events, ask_human, request_approval, cancellation)
  * get_agent_id()   -> the id of the agent calling the tool ("planner" or a spawned agent's id)
  * ToolError        -> raise for expected failures; the message is shown to the model
  * authorize_spend / vault helpers re-exported for convenience

Toolsets are what the planner hands to agents it spawns ("sandbox", "browser", "vercel", your plugin's
toolset, "mcp_linear", …). A plugin's tools default to a toolset named after the plugin file.

Example plugin (drop into ./plugins/slack.py):

    from todd.sdk import todd_tool, get_secret

    @todd_tool(toolset="slack")             # agents spawned with toolsets=["slack"] get it
    async def slack_post(channel: str, text: str) -> str:
        '''Post a message to Slack.

        Args:
            channel: channel name, e.g. #launches
            text: message text
        '''
        token = get_secret("SLACK_TOKEN")
        ...
        return "posted"
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from typing import Any, Callable, Iterable

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as lc_tool

from .policy import SpendDenied, authorize_spend, settle  # noqa: F401  (re-exported)
from .runtime import RunCancelled, RunContext, current_agent_id, set_current_agent  # noqa: F401
from .vault import get_secret, scrub, set_secret  # noqa: F401  (re-exported)

_current_ctx: ContextVar[RunContext | None] = ContextVar("todd_run_ctx", default=None)

TOOLSET_KEY = "todd_toolset"
PLANNER_KEY = "todd_planner"
SOURCE_KEY = "todd_source"


class ToolError(Exception):
    """Expected tool failure; the message is returned to the model instead of crashing the run."""


def get_ctx() -> RunContext:
    ctx = _current_ctx.get()
    if ctx is None:
        raise RuntimeError("get_ctx() called outside of a Todd run")
    return ctx


def set_ctx(ctx: RunContext):
    return _current_ctx.set(ctx)


def get_agent_id() -> str:
    return current_agent_id()


def todd_tool(
    fn: Callable | None = None,
    *,
    toolset: str | None = None,
    planner: bool = False,
    name: str | None = None,
    description: str | None = None,
    parse_docstring: bool = True,
    agents: Iterable[str] | None = None,  # deprecated v0.1 routing: "planner" -> planner=True
    lane: str | None = None,  # deprecated v0.1 option, ignored
):
    """Decorator: turn an (async) function into a LangChain tool.

    toolset: which toolset the tool belongs to (default for plugins: the plugin file name).
    planner: also give the tool directly to the planner (for light, orchestration-level tools).
    Use a Google-style docstring; its summary becomes the tool description and `Args:` become parameter docs.
    """
    if agents is not None:
        agents = list(agents)
        planner = planner or "planner" in agents
        toolset = toolset or next((a for a in agents if a != "planner"), None)

    def deco(f: Callable) -> BaseTool:
        kwargs: dict[str, Any] = {"parse_docstring": parse_docstring and description is None,
                                  "error_on_invalid_docstring": False}
        if description:
            kwargs["description"] = description
        t = lc_tool(name, **kwargs)(f) if name else lc_tool(**kwargs)(f)
        tag(t, toolset=toolset, planner=planner)
        return t

    return deco(fn) if fn is not None else deco


def tag(t: BaseTool, *, toolset: str | None = None, planner: bool | None = None,
        source: str | None = None) -> BaseTool:
    md = dict(t.metadata or {})
    if toolset is not None:
        md[TOOLSET_KEY] = toolset
    if planner is not None:
        md[PLANNER_KEY] = bool(planner)
    if source is not None:
        md.setdefault(SOURCE_KEY, source)
    t.metadata = md
    return t


def toolset_of(t: BaseTool) -> str | None:
    return (t.metadata or {}).get(TOOLSET_KEY)


def for_planner(t: BaseTool) -> bool:
    return bool((t.metadata or {}).get(PLANNER_KEY))


# ----------------------------------------------------------------------------- execution helpers
def to_text(result: Any, limit: int = 12000) -> str:
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, indent=1, default=str)
        except Exception:
            text = str(result)
    if len(text) > limit:
        cut = len(text) - limit
        text = text[: limit // 2] + f"\n…[{cut} chars truncated]…\n" + text[-limit // 2:]
    return text


async def execute_tool_calls(ctx: RunContext, tools: dict[str, BaseTool], calls: list[dict], agent_id: str,
                             quiet: set[str] = frozenset()) -> list[ToolMessage]:
    """Run a batch of tool calls concurrently, emitting timeline events. Never raises (except cancellation)."""

    async def one(call: dict) -> ToolMessage:
        name, args, call_id = call["name"], call.get("args") or {}, call["id"]
        t = tools.get(name)
        if t is None:
            return ToolMessage(f"ERROR: unknown tool {name!r}. Available: {', '.join(tools)}", tool_call_id=call_id,
                               name=name, status="error")
        loud = name not in quiet
        if loud:
            ctx.emit(agent_id, "tool_call", _describe(name, args),
                     {"tool": name, "args": _redact(args), "toolset": toolset_of(t), "call_id": call_id})
        status = "success"
        try:
            text = scrub(to_text(await t.ainvoke(args)))
        except (RunCancelled, asyncio.CancelledError):
            raise
        except SpendDenied as e:
            text, status = f"DENIED: {e}", "error"
        except ToolError as e:
            text, status = f"ERROR: {e}", "error"
        except Exception as e:  # noqa: BLE001 — surface everything to the model
            text, status = f"ERROR: {type(e).__name__}: {e}", "error"
        if status == "error":
            text = scrub(text)
        if loud:
            ctx.emit(agent_id, "tool_result", text[:4000], {"tool": name, "ok": status == "success", "call_id": call_id})
        return ToolMessage(text, tool_call_id=call_id, name=name, status=status)

    return list(await asyncio.gather(*(one(c) for c in calls)))


def _describe(name: str, args: dict[str, Any]) -> str:
    for key in ("name", "task", "domain", "question", "cmd", "path", "url", "key", "text", "summary"):
        v = args.get(key)
        if isinstance(v, str):
            return f"{name}: {v[:300]}"
    return name


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and any(x in k.lower() for x in ("token", "password", "secret", "value")) \
                and not v.startswith("{{secret:"):
            out[k] = v[:2] + "…"
        elif isinstance(v, str) and len(v) > 2000:
            out[k] = v[:2000] + "…"
        else:
            out[k] = v
    return out


__all__ = [
    "RunContext", "RunCancelled", "ToolError", "SpendDenied", "get_ctx", "set_ctx", "get_agent_id", "set_current_agent", "todd_tool",
    "tag", "toolset_of", "for_planner", "authorize_spend", "settle", "get_secret", "set_secret",
    "execute_tool_calls", "to_text",
]
