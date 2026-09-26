"""Per-run context shared by the planner, workers and tools: events, human-in-the-loop, cancellation."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from . import events
from .db import Interaction, add_run_cost, get_run, select, session, update_run, utcnow


class RunCancelled(Exception):
    pass


# The agent (planner or a spawned worker) whose code is currently executing. Set per asyncio task.
_current_agent: ContextVar[str] = ContextVar("todd_agent_id", default="planner")


def current_agent_id() -> str:
    return _current_agent.get()


def set_current_agent(agent_id: str):
    return _current_agent.set(agent_id)


class Inbox(asyncio.Queue):
    """Messages for one agent (from the human or the planner). Besides being a queue, it can be awaited until
    something arrives without taking it out, so a long wait (wait_for_agents) can end early and let the agent read
    the message on its next step."""

    def __init__(self) -> None:
        super().__init__()
        self._arrived = asyncio.Event()

    def put_nowait(self, item: Any) -> None:
        super().put_nowait(item)
        self._arrived.set()

    async def wait_nonempty(self) -> None:
        while self.empty():
            self._arrived.clear()
            await self._arrived.wait()


class PauseGate:
    """Per-agent pause switch. Agents check it between steps (model turns, tool batches, browser steps)."""

    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.running.set()
        self.paused = asyncio.Event()

    @property
    def is_paused(self) -> bool:
        return not self.running.is_set()

    def pause(self) -> None:
        self.running.clear()
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()
        self.running.set()


# interaction_id -> future resolved by the API when a human responds
_pending: dict[str, asyncio.Future] = {}


class RunContext:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.cancelled = False
        self.user_notes = Inbox()  # the human's messages to the planner
        self._waiting = 0
        self.browser_lock = asyncio.Lock()  # one shared browser; browser tasks run one at a time
        self._images: dict[str, str] = {}  # agent_id -> latest screenshot to hand to the model (Claude Code engine)
        self.agents: dict[str, Any] = {}  # agent_id -> AgentHandle (see agents/dynamic.py)
        self.gates: dict[str, PauseGate] = {}  # agent_id -> pause switch ("planner" included)

    # ---- events -------------------------------------------------------------------------
    def emit(self, agent: str, kind: str, text: str, data: dict[str, Any] | None = None) -> None:
        events.emit(self.run_id, agent, kind, text, data)

    def push_image(self, agent_id: str, png_b64: str) -> None:
        self._images[agent_id] = png_b64

    def pop_image(self, agent_id: str) -> str | None:
        return self._images.pop(agent_id, None)

    def add_llm_cost(self, usd: float) -> None:
        if usd:
            add_run_cost(self.run_id, llm=usd)

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise RunCancelled()

    # ---- spawned agents -----------------------------------------------------------------
    def running_agents(self) -> list[Any]:
        return [h for h in self.agents.values() if h.task and not h.task.done()]

    def finish_blocker(self, agent_id: str) -> str | None:
        """Why `agent_id` can't call finish yet: the planner ending would stop every agent that's still running."""
        busy = self.running_agents() if agent_id == "planner" else []
        if not busy:
            return None
        names = ", ".join(f"{h.name} ({h.id})" for h in busy)
        return (f"Not finished: {len(busy)} agent(s) still running: {names}. Call wait_for_agents to collect their "
                "results, or cancel_agent the ones you no longer need, then finish.")

    # ---- pause / resume -----------------------------------------------------------------
    def gate(self, agent_id: str) -> PauseGate:
        if agent_id not in self.gates:
            self.gates[agent_id] = PauseGate()
        return self.gates[agent_id]

    def is_paused(self, agent_id: str) -> bool:
        g = self.gates.get(agent_id)
        return bool(g and g.is_paused)

    def pause_agent(self, agent_id: str, by: str = "you") -> bool:
        g = self.gate(agent_id)
        if g.is_paused:
            return False
        g.pause()
        _set_agent_status(agent_id, "paused")
        self.emit(agent_id, "status", f"Paused by {by} (stops at its next step)", {"agent_status": "paused"})
        return True

    def resume_agent(self, agent_id: str, by: str = "you") -> bool:
        g = self.gates.get(agent_id)
        if not g or not g.is_paused:
            return False
        g.resume()
        _set_agent_status(agent_id, "running")
        self.emit(agent_id, "status", f"Resumed by {by}", {"agent_status": "running"})
        return True

    async def wait_if_paused(self, agent_id: str) -> None:
        """Block here while the agent is paused (called between steps)."""
        g = self.gates.get(agent_id)
        if g and g.is_paused:
            await g.running.wait()
        self.check_cancelled()

    # ---- human in the loop --------------------------------------------------------------
    async def open_interaction(self, kind: str, prompt: str, agent: str, data: dict[str, Any] | None = None) -> str:
        """Create a pending interaction (shown in the dashboard) without waiting for it."""
        self.check_cancelled()
        with session() as s:
            it = Interaction(run_id=self.run_id, agent=agent, kind=kind, prompt=prompt, data=data or {})
            s.add(it)
            s.commit()
            s.refresh(it)
        _pending[it.id] = asyncio.get_running_loop().create_future()
        self.emit(agent, "interaction", prompt, {"interaction_id": it.id, "interaction_kind": kind, **(data or {})})
        update_run(self.run_id, status="waiting")
        return it.id

    async def wait_interaction(self, interaction_id: str, timeout: float | None = None) -> Interaction | None:
        """Wait for a human to resolve an interaction. Returns None on timeout (the interaction stays open)."""
        fut = _pending.get(interaction_id)
        if fut is not None:
            self._waiting += 1
            update_run(self.run_id, status="waiting")
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self._waiting -= 1
                if self._waiting == 0 and not self.cancelled and not _has_pending(self.run_id):
                    update_run(self.run_id, status="running")
            _pending.pop(interaction_id, None)
        self.check_cancelled()
        with session() as s:
            it = s.get(Interaction, interaction_id)
        if it is None or it.status == "pending":
            return None
        return it

    async def _interact(self, kind: str, prompt: str, agent: str, data: dict[str, Any] | None) -> Interaction:
        iid = await self.open_interaction(kind, prompt, agent, data)
        it = await self.wait_interaction(iid)
        assert it is not None
        return it

    async def ask_human(self, question: str, agent: str | None = None, data: dict[str, Any] | None = None) -> str:
        agent = agent or current_agent_id()
        it = await self._interact("question", question, agent, data)
        answer = it.answer or ""
        self.emit(agent, "human_reply", answer or "(no answer)", {"interaction_id": it.id})
        return answer

    async def request_approval(self, prompt: str, agent: str | None = None, data: dict[str, Any] | None = None,
                               kind: str = "approval") -> tuple[bool, str]:
        agent = agent or current_agent_id()
        it = await self._interact(kind, prompt, agent, data)
        approved = it.status == "approved"
        self.emit(agent, "human_reply", ("Approved" if approved else "Denied") + (f": {it.answer}" if it.answer else ""),
                  {"interaction_id": it.id, "approved": approved})
        return approved, it.answer or ""

    def cancel(self) -> None:
        self.cancelled = True
        for g in self.gates.values():  # release paused agents so they can observe the cancellation
            g.resume()
        with session() as s:
            rows = s.exec(
                select(Interaction).where(Interaction.run_id == self.run_id, Interaction.status == "pending")
            ).all()
            for it in rows:
                it.status = "cancelled"
                it.resolved_at = utcnow()
                s.add(it)
            s.commit()
            ids = [it.id for it in rows]
        for i in ids:
            fut = _pending.get(i)
            if fut and not fut.done():
                fut.set_result(None)


def resolve_interaction(interaction_id: str, *, decision: str | None, answer: str | None) -> Interaction | None:
    """Called by the API. decision: approve | deny | None (for plain answers)."""
    with session() as s:
        it = s.get(Interaction, interaction_id)
        if not it or it.status != "pending":
            return it
        if it.kind == "question":
            it.status = "answered"
        else:
            it.status = "approved" if decision == "approve" else "denied"
        it.answer = answer
        it.resolved_at = utcnow()
        s.add(it)
        s.commit()
        s.refresh(it)
    fut = _pending.get(interaction_id)
    if fut and not fut.done():
        fut.set_result(True)
    run = get_run(it.run_id)
    if run and run.status == "waiting" and not _has_pending(it.run_id):
        update_run(it.run_id, status="running")
    return it


def _set_agent_status(agent_id: str, status: str) -> None:
    if agent_id == "planner":
        return
    from .db import AgentInstance

    with session() as s:
        row = s.get(AgentInstance, agent_id)
        if row and row.status in ("running", "paused"):
            row.status = status
            s.add(row)
            s.commit()


def cancel_agent_interactions(run_id: str, agent_id: str) -> None:
    """Close the open questions/approvals of an agent that stopped (so they don't linger in the dashboard)."""
    with session() as s:
        rows = s.exec(select(Interaction).where(Interaction.run_id == run_id, Interaction.agent == agent_id,
                                                Interaction.status == "pending")).all()
        for it in rows:
            it.status = "cancelled"
            it.resolved_at = utcnow()
            s.add(it)
        s.commit()
        ids = [it.id for it in rows]
    for i in ids:
        fut = _pending.pop(i, None)
        if fut and not fut.done():
            fut.cancel()
    run = get_run(run_id)
    if ids and run and run.status == "waiting" and not _has_pending(run_id):
        update_run(run_id, status="running")


def _has_pending(run_id: str) -> bool:
    with session() as s:
        return s.exec(select(Interaction).where(Interaction.run_id == run_id, Interaction.status == "pending")).first() \
            is not None


def cancel_stale_interactions() -> int:
    """At startup: nothing is waiting on interactions from a previous process, so close them."""
    with session() as s:
        rows = s.exec(select(Interaction).where(Interaction.status == "pending")).all()
        for it in rows:
            it.status = "cancelled"
            it.resolved_at = utcnow()
            s.add(it)
        s.commit()
        return len(rows)


def run_exists(run_id: str) -> bool:
    return get_run(run_id) is not None
