"""Spend policy. Every action that costs money goes through authorize_spend(); nothing else can spend.

The policy lives outside the model: a prompt-injected page can ask for money, but it can't approve it.
Rules (any failing rule -> a human must approve):
  * amount <= auto-approve limit
  * amount <= remaining run budget
  * not "always ask"
  * the caller didn't require a human (card payments always do)
  * no identical recent spend in this run (guards against double charges, e.g. after a resume)
"""

from __future__ import annotations

from typing import Any

from . import settings
from .db import LedgerEntry, Run, add_run_cost, get_run, select, session, utcnow
from .runtime import RunContext, current_agent_id

MIN_SPEND_USD = 0.01


class SpendDenied(Exception):
    pass


async def authorize_spend(
    ctx: RunContext,
    *,
    amount_usd: float,
    merchant: str,
    description: str,
    method: str = "account",
    agent: str | None = None,
    data: dict[str, Any] | None = None,
    require_human: bool = False,
) -> LedgerEntry:
    agent = agent or current_agent_id()
    amount = round(float(amount_usd), 2)
    if amount < MIN_SPEND_USD:
        raise SpendDenied(f"Amount must be at least ${MIN_SPEND_USD:.2f}.")
    policy = settings.get("spend_policy")
    limit = float(policy.get("auto_approve_under_usd", 0))
    run = get_run(ctx.run_id)
    assert run is not None
    remaining = round(run.budget_usd - run.spent_usd, 2)

    reasons: list[str] = []
    if amount > remaining:
        reasons.append(f"exceeds remaining run budget (${remaining:.2f} of ${run.budget_usd:.2f})")
    if amount > limit:
        reasons.append(f"over auto-approve limit (${limit:g})")
    if policy.get("always_ask"):
        reasons.append("policy: always ask")
    if require_human:
        reasons.append("this kind of payment always needs approval")
    dup = _duplicate(ctx.run_id, merchant, amount)
    if dup is not None:
        reasons.append(f"possible duplicate of ledger entry #{dup.id} ({dup.status})")

    approved_by = "policy"
    if reasons:
        prompt = f"Approve spending ${amount:.2f} at {merchant}? {description}"
        approved, note = await ctx.request_approval(
            prompt, agent=agent, kind="spend",
            data={"amount_usd": amount, "merchant": merchant, "method": method, "description": description,
                  "why_asking": "; ".join(reasons),
                  **(data or {})},
        )
        if not approved:
            _record(ctx.run_id, merchant, amount, description, method, "denied", "human", data)
            ctx.emit(agent, "status", f"Spend denied: ${amount:.2f} at {merchant}")
            raise SpendDenied(f"The human denied this purchase. {note}".strip())
        approved_by = "human"

    entry = _commit(ctx.run_id, merchant, amount, description, method, approved_by, data)
    ctx.emit(agent, "status", f"Spend authorized ({approved_by}): ${amount:.2f} at {merchant}",
             {"ledger_id": entry.id, "amount_usd": amount, "merchant": merchant})
    return entry


def _commit(run_id: str, merchant: str, amount: float, description: str, method: str, approved_by: str,
            data: dict[str, Any] | None) -> LedgerEntry:
    """Atomically charge the run: row-locked read of the run, re-check, record, update spent/budget."""
    with session() as s:
        run = s.exec(select(Run).where(Run.id == run_id).with_for_update()).one()
        new_spent = round(run.spent_usd + amount, 2)
        if new_spent > run.budget_usd + 1e-9:
            if approved_by != "human":
                raise SpendDenied("Run budget is exhausted (another spend used it first). Ask the human.")
            run.budget_usd = new_spent  # the human approved going over budget
        run.spent_usd = new_spent
        run.updated_at = utcnow()
        e = LedgerEntry(run_id=run_id, merchant=merchant, amount_usd=amount, description=description, method=method,
                        status="authorized", approved_by=approved_by, data=data or {})
        s.add(run)
        s.add(e)
        s.commit()
        s.refresh(e)
        return e


def _duplicate(run_id: str, merchant: str, amount: float) -> LedgerEntry | None:
    with session() as s:
        return s.exec(select(LedgerEntry).where(
            LedgerEntry.run_id == run_id, LedgerEntry.merchant == merchant, LedgerEntry.amount_usd == amount,
            LedgerEntry.status.in_(["authorized", "completed", "needs_review"]),  # type: ignore[attr-defined]
        )).first()


def settle(entry: LedgerEntry, status: str, data: dict[str, Any] | None = None) -> None:
    """Mark an authorized spend completed / failed / voided / needs_review.
    Only failed or voided spends (money definitely not taken) are refunded to the budget."""
    with session() as s:
        row = s.get(LedgerEntry, entry.id)
        if not row:
            return
        row.status = status
        row.data = {**(row.data or {}), **(data or {}), "settled_at": utcnow().isoformat()}
        s.add(row)
        s.commit()
    if status in ("failed", "voided"):
        add_run_cost(entry.run_id, spend=-entry.amount_usd)


def _record(run_id, merchant, amount, description, method, status, approved_by, data) -> LedgerEntry:
    with session() as s:
        e = LedgerEntry(run_id=run_id, merchant=merchant, amount_usd=round(amount, 2), description=description,
                        method=method, status=status, approved_by=approved_by, data=data or {})
        s.add(e)
        s.commit()
        s.refresh(e)
        return e
