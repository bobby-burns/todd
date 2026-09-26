"""Database models and session helpers (SQLModel on Postgres, SQLite for local dev/tests)."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, Session, SQLModel, create_engine, delete, select

from .config import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def short_id() -> str:
    return uuid.uuid4().hex[:12]


class Run(SQLModel, table=True):
    id: str = Field(default_factory=short_id, primary_key=True)
    title: str
    prompt: str = Field(sa_column=Column(Text, nullable=False))
    # queued | running | waiting | succeeded | failed | cancelled | interrupted
    status: str = Field(default="queued", index=True)
    budget_usd: float = 0.0
    spent_usd: float = 0.0
    llm_cost_usd: float = 0.0
    summary: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    ts: datetime = Field(default_factory=utcnow)
    agent: str  # planner | browser | code | infra | system | human
    kind: str  # message | tool_call | tool_result | step | interaction | status | error | cost
    text: str = Field(sa_column=Column(Text, nullable=False))
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class AgentInstance(SQLModel, table=True):
    """An agent the planner spawned for this run (the planner itself is the implicit agent "planner")."""

    id: str = Field(default_factory=lambda: "a_" + uuid.uuid4().hex[:8], primary_key=True)
    run_id: str = Field(index=True)
    name: str
    instructions: str = Field(sa_column=Column(Text, nullable=False))
    task: str = Field(sa_column=Column(Text, nullable=False))
    toolsets: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    model_tier: str = "default"
    model: str = ""
    parent: str = "planner"
    background: bool = False
    # running | succeeded | failed | cancelled | interrupted
    status: str = Field(default="running", index=True)
    summary: str | None = Field(default=None, sa_column=Column(Text))
    llm_cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class Interaction(SQLModel, table=True):
    """Anything that pauses an agent for a human: questions, approvals and spend requests."""

    id: str = Field(default_factory=short_id, primary_key=True)
    run_id: str = Field(index=True)
    agent: str = "planner"
    kind: str  # question | approval | spend
    prompt: str = Field(sa_column=Column(Text, nullable=False))
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="pending", index=True)  # pending | approved | denied | answered | cancelled
    answer: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class Secret(SQLModel, table=True):
    name: str = Field(primary_key=True)
    ciphertext: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: Any = Field(default=None, sa_column=Column(JSON))


class LedgerEntry(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    ts: datetime = Field(default_factory=utcnow)
    merchant: str
    amount_usd: float
    description: str = Field(sa_column=Column(Text, nullable=False))
    method: str = "account"  # account (e.g. Vercel card on file) | card (vault card via browser)
    # authorized | completed | failed | denied | voided
    status: str = "authorized"
    approved_by: str = "policy"  # policy | human
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


_connect_args = {"check_same_thread": False} if config.database_url.startswith("sqlite") else {}
engine = create_engine(config.database_url, connect_args=_connect_args, pool_pre_ping=True)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def session() -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as s:
        yield s


def get_run(run_id: str) -> Run | None:
    with session() as s:
        return s.get(Run, run_id)


def update_run(run_id: str, **fields: Any) -> Run | None:
    with session() as s:
        run = s.get(Run, run_id)
        if not run:
            return None
        for k, v in fields.items():
            setattr(run, k, v)
        run.updated_at = utcnow()
        s.add(run)
        s.commit()
        s.refresh(run)
        return run


def add_run_cost(run_id: str, *, llm: float = 0.0, spend: float = 0.0) -> None:
    with session() as s:
        run = s.get(Run, run_id)
        if not run:
            return
        run.llm_cost_usd = round(run.llm_cost_usd + llm, 6)
        run.spent_usd = round(run.spent_usd + spend, 2)
        run.updated_at = utcnow()
        s.add(run)
        s.commit()


__all__ = [
    "Run",
    "Event",
    "Interaction",
    "AgentInstance",
    "Secret",
    "Setting",
    "LedgerEntry",
    "engine",
    "init_db",
    "session",
    "select",
    "get_run",
    "update_run",
    "add_run_cost",
    "utcnow",
    "short_id",
]
