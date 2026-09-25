"""Dynamic agents: the planner spawns whatever specialists a task needs, at runtime.

An agent = a name + instructions (its role, written by the planner) + a task + toolsets + a model tier.
Each one is its own LangGraph agent with its own event stream (a window in the dashboard). Agents can run in
the foreground (the planner waits for the result) or in the background (the planner keeps working and later
calls wait_for_agents). The human can message or cancel any agent from its window.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from .. import prompts, settings
from ..config import config
from ..db import AgentInstance, Event, get_run, select, session, utcnow
from ..llm import model_for
from ..runtime import RunCancelled, RunContext, cancel_agent_interactions, set_current_agent
from ..sdk import ToolError, get_ctx, todd_tool
from . import claude_code
from .graph import build_agent, recursion_limit

TIERS = ("default", "strong", "fast")


@dataclass
class AgentHandle:
    id: str
    name: str
    toolsets: list[str]
    background: bool
    notes: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    result: dict[str, Any] | None = None


def _update(agent_id: str, **fields: Any) -> None:
    with session() as s:
        row = s.get(AgentInstance, agent_id)
        if row:
            for k, v in fields.items():
                setattr(row, k, v)
            s.add(row)
            s.commit()


def _add_cost(agent_id: str, usd: float) -> None:
    if not usd:
        return
    with session() as s:
        row = s.get(AgentInstance, agent_id)
        if row:
            row.llm_cost_usd = round(row.llm_cost_usd + usd, 6)
            s.add(row)
            s.commit()


def fallback_summary(run_id: str, agent_id: str, status: str, summary: str) -> str:
    """When an agent stops without writing its own summary (cancelled, crashed, turn limit), build one from what
    it actually did so the planner and the human still get a recap."""
    with session() as s:
        evs = s.exec(select(Event).where(Event.run_id == run_id, Event.agent == agent_id)
                     .order_by(Event.id)).all()  # type: ignore[arg-type]
    calls = [e.data.get("tool") for e in evs if e.kind == "tool_call"]
    last = next((e.text for e in reversed(evs) if e.kind in ("thought", "message")), "")
    counts: dict[str, int] = {}
    for c in calls:
        counts[c] = counts.get(c, 0) + 1
    did = ", ".join(f"{k}×{v}" if v > 1 else k for k, v in counts.items()) or "no actions"
    head = {"cancelled": "Stopped before finishing.", "failed": "Didn't finish."}.get(status, "Finished.")
    lines = [f"{head} {summary}".strip(), f"Done so far: {did}."]
    if last:
        lines.append(f"Last step: {last[:300]}")
    return "\n".join(lines)


def running(ctx: RunContext) -> list[AgentHandle]:
    return [h for h in ctx.agents.values() if h.task and not h.task.done()]


async def spawn(ctx: RunContext, *, name: str, instructions: str, task: str, toolsets: list[str],
                model: str = "default", background: bool = False, parent: str = "planner") -> AgentHandle:
    available: dict[str, Any] = getattr(ctx, "toolsets", {}) or {}
    unknown = [t for t in toolsets if t not in available]
    if unknown:
        raise ToolError(f"unknown toolset(s) {unknown}. Available: {', '.join(sorted(available))}")
    if model not in TIERS:
        raise ToolError(f"model must be one of {TIERS}")
    limits = settings.get("limits") or {}
    if len(running(ctx)) >= int(limits.get("max_concurrent_agents", 6)):
        raise ToolError(f"{len(running(ctx))} agents are already running (limit). Wait for some to finish first.")
    if len(ctx.agents) >= int(limits.get("max_agents_per_run", 30)):
        raise ToolError("This run has reached its agent limit. Finish with what you have or ask the human.")

    tools = [t for ts in toolsets for t in available[ts].tools]
    from ..integrations import find_integrations
    from ..tools.human import HUMAN_TOOLS

    seen, uniq = set(), []
    for t in [*tools, find_integrations, *HUMAN_TOOLS]:
        if t.name not in seen:
            uniq.append(t)
            seen.add(t.name)

    run = get_run(ctx.run_id)
    engine = getattr(ctx, "engine", "api")
    model_name = claude_code.model_for(model) if engine == "claude_code" else model_for(model).model
    with session() as s:
        row = AgentInstance(run_id=ctx.run_id, name=name.strip()[:60] or "Agent", instructions=instructions,
                            task=task, toolsets=toolsets, model_tier=model, model=model_name, parent=parent,
                            background=background)
        s.add(row)
        s.commit()
        s.refresh(row)
    handle = AgentHandle(id=row.id, name=row.name, toolsets=toolsets, background=background)
    ctx.agents[row.id] = handle

    system = prompts.compose(
        "worker", agent_name=row.name, instructions=instructions,
        toolsets="\n".join(f"- **{ts}**: {available[ts].description}"
                            + (f"\n  Tips: {available[ts].guide}" if getattr(available[ts], "guide", "") else "")
                            for ts in toolsets) or "- (none)",
        run_goal=(run.prompt if run else "")[:1500],
    )
    graph = None
    if engine != "claude_code":
        graph = build_agent(role=model, agent_id=row.id, agent_name=row.name, system_prompt=system, tools=uniq,
                            notes=handle.notes, on_cost=lambda usd: _add_cost(row.id, usd))
    ctx.emit(parent, "agent_spawned", f"Spawned {row.name}", {
        "agent_id": row.id, "name": row.name, "toolsets": toolsets, "model": model_name, "tier": model,
        "background": background, "task": task[:2000], "instructions": instructions[:2000]})

    async def runner() -> None:
        set_current_agent(row.id)
        ctx.emit(row.id, "status", f"Started · {', '.join(toolsets) or 'no toolsets'} · {model_name}",
                 {"agent_status": "running"})
        status, summary = "failed", ""
        try:
            if graph is None:
                state = await claude_code.run_agent(ctx, role=model, agent_id=row.id, system_prompt=system,
                                                    tools=uniq, first_message=task, notes=handle.notes)
            else:
                state = await graph.ainvoke({"messages": [HumanMessage(task)]},
                                            {"recursion_limit": recursion_limit(config.code_max_steps)})
            ok = bool(state.get("done") and state.get("success"))
            status, summary = ("succeeded" if ok else "failed"), state.get("summary", "")
        except GraphRecursionError:
            summary = f"Stopped: hit the {config.code_max_steps}-turn limit."
        except (RunCancelled, asyncio.CancelledError):
            status, summary = "cancelled", "Cancelled."
        except Exception as e:  # noqa: BLE001
            summary = f"Crashed: {type(e).__name__}: {e}"
            ctx.emit(row.id, "error", summary)
        cancel_agent_interactions(ctx.run_id, row.id)
        if not summary.strip() or status == "cancelled":
            summary = fallback_summary(ctx.run_id, row.id, status, summary)
        handle.result = {"agent_id": row.id, "name": row.name, "status": status, "summary": summary}
        _update(row.id, status=status, summary=summary, finished_at=utcnow())
        ctx.emit(row.id, "summary", summary, {"agent_status": status, "name": row.name})
        ctx.emit(row.id, "status", f"{status.capitalize()}", {"agent_status": status})

    handle.task = asyncio.create_task(runner(), name=f"agent-{row.id}")
    return handle


async def wait(handles: list[AgentHandle], timeout: float | None = None) -> None:
    tasks = {h.task for h in handles if h.task and not h.task.done()}
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)


def describe(h: AgentHandle) -> dict[str, Any]:
    if h.result:
        return h.result
    return {"agent_id": h.id, "name": h.name, "status": "running"}


def cancel_all(ctx: RunContext, reason: str = "") -> int:
    n = 0
    for h in running(ctx):
        h.task.cancel()  # type: ignore[union-attr]
        n += 1
    return n


def mark_interrupted_on_startup() -> None:
    with session() as s:
        rows = s.exec(select(AgentInstance).where(AgentInstance.status == "running")).all()
        for r in rows:
            r.status = "interrupted"
            r.finished_at = utcnow()
            s.add(r)
        s.commit()


def _handle(ctx: RunContext, agent_id: str) -> AgentHandle:
    h = ctx.agents.get(agent_id)
    if h is None:
        with session() as s:
            row = s.get(AgentInstance, agent_id)
        if row and row.run_id == ctx.run_id:
            raise ToolError(f"{row.name} ({agent_id}) is no longer running (status: {row.status}). "
                            f"Its summary: {row.summary or 'none'}. Re-spawn it if you still need it.")
        raise ToolError(f"no agent {agent_id!r} in this run. Use list_agents.")
    return h


# ------------------------------------------------------------------------------------------ planner tools
@todd_tool
async def spawn_agent(name: str, instructions: str, task: str, toolsets: list[str], model: str = "default",
                      background: bool = False, tasks: list[str] | None = None) -> dict:
    """Create an agent for a coherent chunk of the work. One agent should own a group of RELATED tasks that share
    context and tools (use `tasks` for the checklist); don't create one agent per small step. Design it for the
    job: a short name (e.g. "Store Listing", "Marketing Site & Domain"), instructions describing its role,
    standards and constraints, and only the toolsets it needs — API/MCP toolsets before `browser`. Agents don't see your conversation: the task
    must be self-contained (inputs, exact outputs to report, done condition).
    Foreground (background=false) waits and returns the agent's summary. Background returns immediately with
    an agent_id; spawn several background agents in one turn to run them in parallel, then wait_for_agents.

    Args:
        name: short human-readable agent name
        instructions: the agent's role and rules (becomes its system prompt)
        task: the concrete assignment with all needed context and what to report back
        toolsets: toolsets to give it, e.g. ["sandbox", "github"] or ["browser", "accounts"]
        model: "default", "strong" (hard reasoning) or "fast" (simple, cheap tasks)
        background: run without waiting (default false)
        tasks: optional checklist of related sub-tasks this one agent should complete, in order
    """
    ctx = get_ctx()
    if tasks:
        task = task.rstrip() + "\n\nChecklist (do all of these, in order):\n" + "\n".join(f"{i}. {t}" for i, t in
                                                                                         enumerate(tasks, 1))
    h = await spawn(ctx, name=name, instructions=instructions, task=task, toolsets=list(toolsets),
                    model=model, background=background)
    if background:
        return {"agent_id": h.id, "name": h.name, "status": "running",
                "note": "Running in the background. Call wait_for_agents to collect its result."}
    await wait([h])
    return describe(h)


@todd_tool
async def wait_for_agents(agent_ids: list[str] | None = None, timeout_s: int | None = None) -> dict:
    """Wait for background agents to finish and return their results. Omit agent_ids to wait for all running
    agents. With timeout_s, returns early with the status of each (unfinished ones show "running").

    Args:
        agent_ids: which agents to wait for (default: all running)
        timeout_s: maximum seconds to wait
    """
    ctx = get_ctx()
    handles = [_handle(ctx, i) for i in agent_ids] if agent_ids else running(ctx)
    await wait(handles, timeout=timeout_s)
    return {"agents": [describe(h) for h in handles]}


@todd_tool
async def message_agent(agent_id: str, message: str) -> str:
    """Send a message to a running agent (new info, a correction, a changed priority). It reads it on its next
    turn.

    Args:
        agent_id: the agent's id
        message: what to tell it
    """
    ctx = get_ctx()
    h = _handle(ctx, agent_id)
    if not h.task or h.task.done():
        raise ToolError(f"{h.name} has already finished.")
    h.notes.put_nowait(f"[from planner] {message}")
    ctx.emit(agent_id, "note", message, {"from": "planner"})
    return f"Sent to {h.name}."


@todd_tool
async def cancel_agent(agent_id: str, reason: str = "") -> str:
    """Stop a running agent.

    Args:
        agent_id: the agent's id
        reason: why (shown in its window)
    """
    ctx = get_ctx()
    h = _handle(ctx, agent_id)
    if h.task and not h.task.done():
        ctx.emit(agent_id, "status", f"Cancelled by planner{': ' + reason if reason else ''}")
        h.task.cancel()
        await wait([h], timeout=10)
    return f"{h.name}: {describe(h)['status']}"


@todd_tool
async def list_agents() -> dict:
    """List every agent spawned in this run with its status and summary."""
    ctx = get_ctx()
    with session() as s:
        rows = s.exec(select(AgentInstance).where(AgentInstance.run_id == ctx.run_id)
                      .order_by(AgentInstance.created_at)).all()  # type: ignore[arg-type]
    return {"agents": [{"agent_id": r.id, "name": r.name, "status": r.status, "toolsets": r.toolsets,
                        "summary": r.summary} for r in rows]}


ORCHESTRATION_TOOLS = [spawn_agent, wait_for_agents, message_agent, cancel_agent, list_agents]
