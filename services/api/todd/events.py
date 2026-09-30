"""Event bus: every agent action is persisted as an Event and fanned out to live subscribers (SSE)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from .db import Event, session

_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
_global_subscribers: set[asyncio.Queue] = set()


def serialize(ev: Event) -> dict[str, Any]:
    return {
        "id": ev.id,
        "run_id": ev.run_id,
        "ts": ev.ts.isoformat(),
        "agent": ev.agent,
        "kind": ev.kind,
        "text": ev.text,
        "data": ev.data or {},
    }


def _clean(value: Any) -> Any:
    """What the timeline stores and shows never holds a secret: vault values and secret-looking strings are hidden."""
    from . import redact, vault

    return redact.deep(value, vault.scrub)


def emit(run_id: str, agent: str, kind: str, text: str, data: dict[str, Any] | None = None) -> dict:
    ev = Event(run_id=run_id, agent=agent, kind=kind, text=_clean(text[:20000]), data=_clean(_jsonable(data or {})))
    with session() as s:
        s.add(ev)
        s.commit()
        s.refresh(ev)
    payload = serialize(ev)
    for q in list(_subscribers.get(run_id, ())) + list(_global_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
    return payload


def subscribe(run_id: str | None = None) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    if run_id is None:
        _global_subscribers.add(q)
    else:
        _subscribers[run_id].add(q)
    return q


def unsubscribe(q: asyncio.Queue, run_id: str | None = None) -> None:
    if run_id is None:
        _global_subscribers.discard(q)
    else:
        _subscribers[run_id].discard(q)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
