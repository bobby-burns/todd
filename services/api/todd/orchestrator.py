"""Run manager: builds the planner graph for a run, executes it with a checkpointer, supports resume/cancel."""

from __future__ import annotations

import asyncio
import logging
import traceback
import uuid
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from . import accounts, connect, prompts, registry, settings, vault
from .agents import claude_code, dynamic
from .agents.graph import build_agent, recursion_limit
from .config import config
from .db import Run, get_run, select, session, update_run
from .runtime import RunCancelled, RunContext, cancel_stale_interactions
from .sdk import set_ctx

log = logging.getLogger("todd.orchestrator")


def integrations_summary() -> str:
    cfg = settings.get_all()
    contact = cfg["registrant_contact"]
    contact_ok = all(contact.get(k) for k in ("firstName", "lastName", "email", "phone", "address1", "city", "zip"))
    lines = [
        f"- Vercel: {'connected' if vault.has_secret('VERCEL_TOKEN') else 'NOT configured'}"
        + (f" (team {cfg['integrations']['vercel_team_id']})" if cfg['integrations'].get('vercel_team_id') else ""),
        f"- GitHub: {'connected' if vault.has_secret('GITHUB_TOKEN') else 'NOT configured'}",
        f"- Domain registrant contact: {'complete' if contact_ok else 'incomplete (domain purchases will fail)'}",
        f"- Payment card for browser checkouts: {'stored' if vault.has_secret('CARD_NUMBER') else 'none'}",
        "- CLIs signed in with the human's account: " + (", ".join(c.name for c in connect.CONNECTORS.values()
                                                                    if connect.is_connected(c.service)) or "none")
        + " (others can be connected with cli_login: " + ", ".join(
            c.service for c in connect.CONNECTORS.values() if not connect.is_connected(c.service)) + ")",
        f"- Vault secrets: {', '.join(x['name'] for x in vault.list_secrets()) or 'none'}",
        "- Model tiers: " + ", ".join(f"{k}={v}" for k, v in cfg["models"].items()),
    ]
    return "\n".join(lines)


async def accounts_summary() -> str:
    """Quick (cookie/remembered) status of the accounts the human set up; no page probes."""
    selected = settings.get("accounts_selected") or []
    if not selected:
        return "- Accounts: the human hasn't picked any yet (use check_accounts for the services you need)."
    try:
        st = await accounts.statuses(selected)
    except Exception:
        return "- Accounts: status unavailable."
    if not st["browser_online"]:
        return "- Accounts: browser offline."
    groups: dict[str, list[str]] = {}
    for a in st["accounts"]:
        groups.setdefault(a["status"], []).append(a["name"])
    return "\n".join(f"- Accounts {k.replace('_', ' ')}: {', '.join(v)}" for k, v in groups.items())


def run_engine(run_id: str) -> str | None:
    """The engine a run started with (recorded on its first status event)."""
    from .db import Event

    with session() as s:
        evs = s.exec(select(Event).where(Event.run_id == run_id, Event.agent == "system", Event.kind == "status")
                     .order_by(Event.id).limit(5)).all()  # type: ignore[arg-type]
    return next((e.data.get("engine") for e in evs if (e.data or {}).get("engine")), None)


class RunManager:
    def __init__(self) -> None:
        self.contexts: dict[str, RunContext] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.checkpointer: Any = None
        self._stack = AsyncExitStack()

    # ---- lifecycle ------------------------------------------------------------------------
    async def startup(self) -> None:
        self.checkpointer = await self._make_checkpointer()
        with session() as s:
            stale = s.exec(select(Run).where(Run.status.in_(["running", "waiting", "queued"]))).all()  # type: ignore
            for r in stale:
                r.status = "interrupted"
                s.add(r)
            s.commit()
        cancel_stale_interactions()
        dynamic.mark_interrupted_on_startup()

    async def shutdown(self) -> None:
        for rid in list(self.tasks):
            self.cancel(rid)
        await self._stack.aclose()

    async def _make_checkpointer(self) -> Any:
        url = config.database_url
        if url.startswith("postgresql"):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            conn = url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://",
                                                                                "postgresql://")
            saver = await self._stack.enter_async_context(AsyncPostgresSaver.from_conn_string(conn))
            await saver.setup()
            return saver
        if url.startswith("sqlite"):
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            path = str(config.data_dir / "checkpoints.sqlite")
            return await self._stack.enter_async_context(AsyncSqliteSaver.from_conn_string(path))
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()

    # ---- control --------------------------------------------------------------------------
    def start(self, run_id: str, resume: bool = False) -> None:
        if run_id in self.tasks and not self.tasks[run_id].done():
            return
        ctx = RunContext(run_id)
        self.contexts[run_id] = ctx
        self.tasks[run_id] = asyncio.create_task(self._execute(ctx, resume), name=f"run-{run_id}")

    def cancel(self, run_id: str) -> bool:
        ctx = self.contexts.get(run_id)
        task = self.tasks.get(run_id)
        if ctx:
            ctx.cancel()
            dynamic.cancel_all(ctx)
        if task and not task.done():
            task.cancel()
            return True
        run = get_run(run_id)
        if run and run.status in ("interrupted", "queued"):
            update_run(run_id, status="cancelled")
            return True
        return False

    def note(self, run_id: str, text: str, agent_id: str = "planner") -> bool:
        """Human message to the planner or to a specific running agent. It's read on the agent's next step, ends a
        wait_for_agents early, and resumes the agent if it was paused."""
        ctx = self.contexts.get(run_id)
        if not ctx or run_id not in self.tasks or self.tasks[run_id].done():
            return False
        if agent_id == "planner":
            ctx.user_notes.put_nowait(f"[from the human] {text}")
        else:
            h = ctx.agents.get(agent_id)
            if not h or not h.task or h.task.done():
                return False
            h.notes.put_nowait(f"[from the human] {text}")
        ctx.emit(agent_id, "note", text, {"from": "human"})
        ctx.resume_agent(agent_id, by="your message")  # messaging a paused agent is the go-ahead to continue
        return True

    def pause(self, run_id: str, agent_id: str | None = None, resume: bool = False) -> int:
        """Pause/resume one agent ("planner" included) or, with agent_id=None, the planner and every running agent."""
        ctx = self.contexts.get(run_id)
        if not ctx or not self.is_active(run_id):
            return 0
        ids = [agent_id] if agent_id else ["planner", *[h.id for h in dynamic.running(ctx)]]
        n = 0
        for i in ids:
            if i != "planner" and (i not in ctx.agents or not ctx.agents[i].task or ctx.agents[i].task.done()):
                continue
            n += ctx.resume_agent(i) if resume else ctx.pause_agent(i)
        return n

    def is_paused(self, run_id: str, agent_id: str) -> bool:
        ctx = self.contexts.get(run_id)
        return bool(ctx and self.is_active(run_id) and ctx.is_paused(agent_id))

    def cancel_agent(self, run_id: str, agent_id: str) -> bool:
        ctx = self.contexts.get(run_id)
        h = ctx.agents.get(agent_id) if ctx else None
        if not h or not h.task or h.task.done():
            return False
        ctx.emit(agent_id, "status", "Cancelled by you")  # type: ignore[union-attr]
        h.task.cancel()
        return True

    def is_active(self, run_id: str) -> bool:
        t = self.tasks.get(run_id)
        return bool(t and not t.done())

    # ---- execution ------------------------------------------------------------------------
    async def _execute(self, ctx: RunContext, resume: bool) -> None:
        run_id = ctx.run_id
        set_ctx(ctx)  # task-local: visible to every tool running inside this run
        run = get_run(run_id)
        assert run is not None
        update_run(run_id, status="running")
        engine = settings.get("engine") if settings.get("engine") in ("api", "claude_code") else "claude_code"
        if resume:
            engine = run_engine(run_id) or engine  # a resumed run keeps the engine it started with
        ctx.engine = engine  # type: ignore[attr-defined]  # fixed for the life of the run
        ctx.emit("system", "status", "Run resumed from last checkpoint" if resume else "Run started",
                 {"engine": engine})
        try:
            mcp_toolsets, mcp_errors = await registry.load_mcp_toolsets()
            for e in mcp_errors:
                ctx.emit("system", "error", f"MCP server {e['server']} failed to load: {e['error']}")
            ctx.toolsets = registry.all_toolsets(mcp_toolsets, engine=engine)  # type: ignore[attr-defined]
            system = prompts.compose(
                "planner", budget_usd=f"{run.budget_usd:.2f}",
                integrations=integrations_summary() + "\n" + await accounts_summary(),
                toolsets=registry.toolsets_prompt(ctx.toolsets))  # type: ignore[attr-defined]
            planner_tools = registry.planner_tools(ctx.toolsets)  # type: ignore[attr-defined]
            if engine == "claude_code":
                state = await self._planner_claude_code(ctx, run, system, planner_tools, resume)
            else:
                graph = build_agent(role="planner", agent_id="planner", agent_name="Planner", system_prompt=system,
                                    tools=planner_tools, notes=ctx.user_notes, checkpointer=self.checkpointer)
                cfg = {"configurable": {"thread_id": run_id},
                       "recursion_limit": recursion_limit(config.planner_max_turns)}
                inp: Any = {"messages": [HumanMessage(run.prompt)]}
                if resume:
                    snap = await graph.aget_state(cfg)
                    if snap.values.get("messages"):
                        inp = None  # continue from the last checkpoint
                    else:
                        ctx.emit("system", "status", "No checkpoint yet; starting from the prompt")
                state = await graph.ainvoke(inp, cfg)
            ok = bool(state.get("done") and state.get("success"))
            summary = state.get("summary") or ""
            if not summary.strip():
                summary = dynamic.fallback_summary(run_id, "planner", "succeeded" if ok else "failed", "")
            update_run(run_id, status="succeeded" if ok else "failed", summary=summary)
            ctx.emit("planner", "summary", summary, {"agent_status": "succeeded" if ok else "failed", "run": True})
            ctx.emit("system", "status", "Run finished" if ok else "Run ended without completing the goal",
                     {"summary": summary, "success": ok})
        except (RunCancelled, asyncio.CancelledError):
            update_run(run_id, status="cancelled")
            ctx.emit("system", "status", "Run cancelled")
        except claude_code.ClaudeCodeError as e:
            update_run(run_id, status="failed", summary=str(e))
            ctx.emit("system", "error", str(e))
        except GraphRecursionError:
            update_run(run_id, status="failed", summary=f"Hit the {config.planner_max_turns}-turn limit.")
            ctx.emit("system", "error", f"Stopped: hit the {config.planner_max_turns}-turn limit. You can resume it.")
        except Exception as e:  # noqa: BLE001
            log.exception("run %s crashed", run_id)
            update_run(run_id, status="failed", summary=f"{type(e).__name__}: {e}")
            ctx.emit("system", "error", f"{type(e).__name__}: {e}", {"traceback": traceback.format_exc(limit=8)})
        finally:
            n = dynamic.cancel_all(ctx)
            if n:
                ctx.emit("system", "status", f"Stopped {n} agent(s) still running when the planner ended")
                await asyncio.sleep(0)
            self.tasks.pop(run_id, None)


    async def _planner_claude_code(self, ctx: RunContext, run: Run, system: str, tools: list, resume: bool) -> dict:
        """The planner as a Claude Code session. Its session id is derived from the run id, so Resume continues the
        same conversation after a restart."""
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"todd-run-{run.id}"))
        exists = claude_code.session_exists(sid)
        first = run.prompt
        if resume and exists:
            first = ("Todd restarted and this run was interrupted. Continue from where you left off. Agents that "
                     "were running have stopped: check list_agents and re-spawn what's still needed.")
        elif resume:
            ctx.emit("system", "status", "No checkpoint yet; starting from the prompt")
        return await claude_code.run_agent(ctx, role="planner", agent_id="planner", system_prompt=system,
                                           tools=tools, first_message=first, notes=ctx.user_notes,
                                           session_id=sid, resume=resume and exists,
                                           max_calls=config.planner_max_turns * 4)


manager = RunManager()
