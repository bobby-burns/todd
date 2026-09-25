"""Shared helpers for the test suite: a scripted fake chat model and run utilities."""

from __future__ import annotations

import asyncio
import itertools
import os
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from todd.db import Event, Interaction, Run, get_run, select, session
from todd.orchestrator import manager

_ids = itertools.count()


def call(_tool: str, **args: Any) -> dict:
    return {"name": _tool, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


class ScriptedModel(BaseChatModel):
    """Pops the next scripted AIMessage for this agent (by agent name, falling back to role/tier).
    A script item may be an AIMessage, an Exception to raise, or a callable(messages) -> AIMessage."""

    role: str
    agent_name: str | None = None
    scripts: dict

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kw):  # noqa: D401
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        queue = self.scripts.get(self.agent_name or "", self.scripts.get(self.role, []))
        if not queue:
            item: Any = AIMessage("", tool_calls=[call("finish", summary="script exhausted", success=False)])
        else:
            item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(messages)
        return ChatResult(generations=[ChatGeneration(message=item)])


SCRIPTS: dict[str, list] = {"planner": []}


def agents_of(run_id: str) -> list:
    from todd.db import AgentInstance

    with session() as s:
        return list(s.exec(select(AgentInstance).where(AgentInstance.run_id == run_id)
                           .order_by(AgentInstance.created_at)).all())


def new_run(prompt: str, budget: float = 50) -> str:
    with session() as s:
        r = Run(title=prompt[:40], prompt=prompt, budget_usd=budget)
        s.add(r)
        s.commit()
        return r.id


async def wait_status(run_id: str, statuses: set[str], timeout: float = 30) -> Run:
    for _ in range(int(timeout * 20)):
        r = get_run(run_id)
        if r and r.status in statuses and (r.status == "waiting" or not manager.is_active(run_id)):
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} stuck in {get_run(run_id).status}")


async def pending(run_id: str, timeout: float = 10) -> Interaction:
    for _ in range(int(timeout * 20)):
        with session() as s:
            it = s.exec(select(Interaction).where(Interaction.run_id == run_id,
                                                  Interaction.status == "pending")).first()
        if it:
            return it
        await asyncio.sleep(0.05)
    raise AssertionError("no pending interaction")


def events_of(run_id: str) -> list[Event]:
    with session() as s:
        return list(s.exec(select(Event).where(Event.run_id == run_id).order_by(Event.id)).all())


def sandbox_up() -> bool:
    try:
        return httpx.get(os.getenv("SANDBOX_URL", "http://sandbox:7000") + "/health", timeout=1).status_code == 200
    except Exception:
        return False


