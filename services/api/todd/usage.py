"""Usage: what each run used, for the dashboard's usage views.

Model calls are summed as they happen (`record`, one row per run, agent, model and day), from both engines: the API
engine's LangChain usage (billed to your API keys, `cost_usd`) and Claude Code's stream (your Claude plan: tokens,
plus what the same usage would cost at API prices, `plan_usd`). Tool calls and browser steps are counted from the
run's events, so they need no extra bookkeeping.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import AgentInstance, Event, Run, UsageTotal, select, session, utcnow

def _day(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def record(run_id: str, agent_id: str, model: str = "", *, calls: int = 1, input_tokens: int = 0,
           output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0, cost_usd: float = 0.0,
           plan_usd: float = 0.0) -> None:
    """Add one model call's (or a batch's) usage to today's totals for this run, agent and model."""
    if not (calls or input_tokens or output_tokens or cost_usd or plan_usd):
        return
    day = _day()
    key = f"{run_id}:{agent_id}:{model}:{day}"
    with session() as s:
        row = s.get(UsageTotal, key) or UsageTotal(id=key, run_id=run_id, agent_id=agent_id, model=model, day=day)
        row.calls += int(calls or 0)
        row.input_tokens += int(input_tokens or 0)
        row.output_tokens += int(output_tokens or 0)
        row.cache_read_tokens += int(cache_read_tokens or 0)
        row.cache_write_tokens += int(cache_write_tokens or 0)
        row.cost_usd = round(row.cost_usd + float(cost_usd or 0), 6)
        row.plan_usd = round(row.plan_usd + float(plan_usd or 0), 6)
        row.updated_at = utcnow()
        s.add(row)
        s.commit()


def from_langchain(usage: dict | None) -> dict[str, int]:
    """LangChain usage_metadata → record() keywords."""
    if not usage:
        return {}
    details = usage.get("input_token_details") or {}
    cache_read = int(details.get("cache_read") or 0)
    cache_write = int(details.get("cache_creation") or 0)
    return {"input_tokens": max(0, int(usage.get("input_tokens") or 0) - cache_read - cache_write),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": cache_read, "cache_write_tokens": cache_write}


def from_anthropic(usage: dict | None) -> dict[str, int]:
    """Anthropic-style usage (Claude Code's stream) → record() keywords."""
    if not usage:
        return {}
    return {"input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0)}


FIELDS = ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd", "plan_usd")


def _sum(rows: list[UsageTotal]) -> dict[str, Any]:
    out: dict[str, Any] = {f: 0 for f in FIELDS}
    for r in rows:
        for f in FIELDS:
            out[f] += getattr(r, f)
    out["cost_usd"] = round(out["cost_usd"], 4)
    out["plan_usd"] = round(out["plan_usd"], 4)
    out["tokens"] = out["input_tokens"] + out["output_tokens"] + out["cache_read_tokens"] + out["cache_write_tokens"]
    return out


def _secs(a: datetime | None, b: datetime | None) -> int | None:
    if not a or not b:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return max(0, int((b - a).total_seconds()))


def run_usage(run_id: str) -> dict[str, Any]:
    """Everything a run used: totals, per agent (model usage, tool calls, browser steps, time) and per model/tool."""
    with session() as s:
        run = s.get(Run, run_id)
        rows = s.exec(select(UsageTotal).where(UsageTotal.run_id == run_id)).all()
        agents = s.exec(select(AgentInstance).where(AgentInstance.run_id == run_id)).all()
        evs = s.exec(select(Event.agent, Event.kind, Event.data).where(  # type: ignore[call-overload]
            Event.run_id == run_id, Event.kind.in_(("tool_call", "browser_step")))).all()  # type: ignore[attr-defined]
    tools: Counter[str] = Counter()
    per_agent_tools: dict[str, Counter[str]] = defaultdict(Counter)
    steps: Counter[str] = Counter()
    for agent, kind, data in evs:
        a = "planner" if agent in ("system", "human") else agent
        if kind == "browser_step":
            steps[a] += 1
        else:
            name = str((data or {}).get("tool") or "?")
            tools[name] += 1
            per_agent_tools[a][name] += 1
    by_agent: dict[str, list[UsageTotal]] = defaultdict(list)
    by_model: dict[str, list[UsageTotal]] = defaultdict(list)
    for r in rows:
        by_agent[r.agent_id].append(r)
        by_model[r.model or "unknown"].append(r)
    # a run still going counts up to now; a finished one to its last activity
    run_end = (utcnow() if run.status in ("queued", "running", "waiting") else run.updated_at) if run else None
    names = {"planner": "Planner", **{a.id: a.name for a in agents}}
    info = {a.id: a for a in agents}
    ids = ["planner", *[a.id for a in agents]]
    ids += [i for i in {*by_agent, *per_agent_tools, *steps} if i not in ids]
    agent_rows = []
    for aid in ids:
        a = info.get(aid)
        agent_rows.append({
            "id": aid, "name": names.get(aid, aid), "status": a.status if a else (run.status if run else None),
            "model": a.model if a else "", **_sum(by_agent.get(aid, [])),
            "tool_calls": sum(per_agent_tools[aid].values()), "browser_steps": steps[aid],
            "top_tools": per_agent_tools[aid].most_common(5),
            "seconds": _secs(a.created_at, a.finished_at or utcnow()) if a else
            (_secs(run.created_at, run_end) if run else None),
        })
    total = _sum(list(rows))
    total.update(tool_calls=sum(tools.values()), browser_steps=sum(steps.values()), agents=len(agents),
                 seconds=_secs(run.created_at, run_end) if run else None,
                 spent_usd=run.spent_usd if run else 0.0)
    return {
        "run_id": run_id, "total": total, "agents": agent_rows,
        "models": sorted(({"model": m, **_sum(rs)} for m, rs in by_model.items()), key=lambda x: -x["tokens"]),
        "tools": [{"tool": t, "calls": n} for t, n in tools.most_common(12)],
    }


def overview(days: int = 30) -> dict[str, Any]:
    """Usage across runs: per day for the last `days` days, the runs that used the most, and per model."""
    days = max(1, min(int(days), 365))
    since = _day(datetime.now(timezone.utc) - timedelta(days=days - 1))
    with session() as s:
        rows = s.exec(select(UsageTotal).where(UsageTotal.day >= since)).all()
        run_ids = {r.run_id for r in rows}
        titles = {r.id: (r.title, r.status, r.created_at) for r in
                  s.exec(select(Run).where(Run.id.in_(run_ids))).all()} if run_ids else {}  # type: ignore[attr-defined]
    by_day: dict[str, list[UsageTotal]] = defaultdict(list)
    by_run: dict[str, list[UsageTotal]] = defaultdict(list)
    by_model: dict[str, list[UsageTotal]] = defaultdict(list)
    for r in rows:
        by_day[r.day].append(r)
        by_run[r.run_id].append(r)
        by_model[r.model or "unknown"].append(r)
    start = datetime.now(timezone.utc) - timedelta(days=days - 1)
    series = [{"day": d, **_sum(by_day.get(d, []))} for d in (_day(start + timedelta(days=i)) for i in range(days))]
    runs = []
    for rid, rs in by_run.items():
        t = titles.get(rid)
        runs.append({"run_id": rid, "title": t[0] if t else "A deleted run", "status": t[1] if t else None,
                     "created_at": t[2] if t else None, "exists": bool(t), **_sum(rs)})
    runs.sort(key=lambda x: -x["tokens"])
    return {"days": days, "total": _sum(list(rows)), "series": series, "runs": runs[:50],
            "models": sorted(({"model": m, **_sum(rs)} for m, rs in by_model.items()), key=lambda x: -x["tokens"])}
