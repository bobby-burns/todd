"""The LangGraph agent used by the planner and by every agent it spawns.

    START → model ─┬─(tool calls)→ tools ─┬─(finish called)→ END
                   │                      └────────────────→ model
                   └─(no tool calls)→ nudge → model  (gives up after 3 nudges)

* All tool calls from one model turn run concurrently.
* Messages for this agent (from the human or the planner) are drained from `notes` before each model turn.
* Native reasoning (reasoning_content) is emitted as "thinking"; text written alongside tool calls is emitted as
  a "thought" (agents are prompted to think out loud), and text without tool calls as a "message".
* With a checkpointer, every step is persisted under the thread id, so the run can be resumed.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .. import llm
from ..sdk import execute_tool_calls, get_ctx, todd_tool


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    done: bool
    success: bool
    summary: str
    nudges: int


@todd_tool
async def finish(summary: str, success: bool = True) -> str:
    """Finish your task with a short recap. Call this once everything is done (or you cannot continue).

    Args:
        summary: recap as plain lines — "Done: …", "Outputs: …" (URLs, IDs, paths, values), "How: …" (APIs/MCPs/CLIs,
            browser only if needed), "Left / needs you: …"
        success: whether the task was fully achieved
    """
    return "ok"


def message_text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts).strip()


def reasoning_text(msg: Any) -> str:
    kw = getattr(msg, "additional_kwargs", {}) or {}
    r = kw.get("reasoning_content") or ""
    if not r:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            r = "\n".join(b.get("thinking", "") for b in content if isinstance(b, dict) and b.get("type") == "thinking")
    return str(r).strip()


def _compact_for_model(messages: list[AnyMessage], keep_recent: int = 30, max_chars: int = 1500) -> list[AnyMessage]:
    """Bound context: shrink old tool outputs (state keeps full history; only the model's view is trimmed)."""
    if len(messages) <= keep_recent:
        return messages
    out: list[AnyMessage] = []
    cutoff = len(messages) - keep_recent
    for i, m in enumerate(messages):
        if i < cutoff and isinstance(m, ToolMessage) and isinstance(m.content, str) and len(m.content) > max_chars:
            m = m.model_copy(update={"content": m.content[:max_chars] + "\n…[older output trimmed]"})
        out.append(m)
    return out


def build_agent(
    *,
    role: str,
    agent_id: str,
    system_prompt: str,
    tools: list[BaseTool],
    notes: asyncio.Queue | None = None,
    agent_name: str | None = None,
    checkpointer: Any = None,
    on_cost: Any = None,
):
    all_tools = [*tools, finish]
    tool_map = {t.name: t for t in all_tools}
    model = llm.make_chat_model(role, agent_name).bind_tools(all_tools)

    async def call_model(state: AgentState) -> dict:
        ctx = get_ctx()
        await ctx.wait_if_paused(agent_id)  # a paused agent stops here, between steps
        new: list[AnyMessage] = []
        if notes is not None:
            incoming = []
            while not notes.empty():
                incoming.append(notes.get_nowait())
            if incoming:
                new.append(HumanMessage("New message(s) while you work:\n" + "\n".join(incoming)))
        history = _compact_for_model([*state["messages"], *new])
        ai: AIMessage = await model.ainvoke([SystemMessage(system_prompt), *history])
        cost = llm.cost_of(role, getattr(ai, "usage_metadata", None))
        ctx.add_llm_cost(cost)
        if on_cost:
            on_cost(cost)
        thinking = reasoning_text(ai)
        if thinking:
            ctx.emit(agent_id, "thinking", thinking)
        text = message_text(ai)
        if text:
            ctx.emit(agent_id, "thought" if getattr(ai, "tool_calls", None) else "message", text)
        return {"messages": [*new, ai]}

    async def call_tools(state: AgentState) -> dict:
        ctx = get_ctx()
        await ctx.wait_if_paused(agent_id)  # don't start the next actions while paused
        ai = state["messages"][-1]
        calls = list(getattr(ai, "tool_calls", []) or [])
        fin = next((c for c in calls if c["name"] == "finish"), None)
        others = [c for c in calls if c is not fin]
        results = await execute_tool_calls(ctx, tool_map, others, agent_id)
        update: dict[str, Any] = {"messages": results, "nudges": 0}
        blocked = ctx.finish_blocker(agent_id) if fin is not None else None
        if blocked:
            ctx.emit(agent_id, "status", "Can't finish yet: agents are still running")
            update["messages"] = [*results, ToolMessage(blocked, tool_call_id=fin["id"], name="finish", status="error")]
        elif fin is not None:
            args = fin.get("args") or {}
            update["messages"] = [*results, ToolMessage("ok", tool_call_id=fin["id"], name="finish")]
            update.update(done=True, success=bool(args.get("success", True)), summary=str(args.get("summary", "")))
        return update

    async def nudge(state: AgentState) -> dict:
        n = int(state.get("nudges", 0)) + 1
        if n >= 3:
            last = message_text(state["messages"][-1])
            return {"nudges": n, "done": True, "success": False, "summary": last or "Stopped without finishing."}
        return {"nudges": n, "messages": [HumanMessage(
            "Continue working using your tools. When everything is done, call `finish` with a summary.")]}

    def after_model(state: AgentState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else "nudge"

    def after_tools(state: AgentState) -> str:
        return END if state.get("done") else "model"

    def after_nudge(state: AgentState) -> str:
        return END if state.get("done") else "model"

    g = StateGraph(AgentState)
    g.add_node("model", call_model)
    g.add_node("tools", call_tools)
    g.add_node("nudge", nudge)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", after_model, ["tools", "nudge"])
    g.add_conditional_edges("tools", after_tools, ["model", END])
    g.add_conditional_edges("nudge", after_nudge, ["model", END])
    return g.compile(checkpointer=checkpointer, name=f"todd-{agent_id}")


def recursion_limit(max_turns: int) -> int:
    # each turn is model + tools (2 supersteps); leave headroom for nudges
    return max_turns * 2 + 10
