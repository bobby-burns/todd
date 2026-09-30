"""The launch plan: where something Todd builds will live, and how it goes public.

A goal like "make a website that…" ends somewhere people can use it, so Todd asks three things at the start, on one
card, without stopping the run (agents start building while the human decides):

* **Address:** their own domain (connected at their registrar), a new domain (Todd checks prices; buying still asks),
  or a free address for now (e.g. name.vercel.app).
* **Code:** a private GitHub repository (the default) or a public one.
* **Going live:** ask them first (the default) or put it live when it's ready.

The answer goes to the planner as a message and is enforced in code (gates.py): deploys (previews too) and connecting a
domain wait for the human unless they chose "when it's ready", and a public repository needs them to have chosen
public (or to approve it). Until they answer, the defaults apply.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from .runtime import RunContext

log = logging.getLogger("todd.launch")

DEFAULT: dict[str, str] = {"domain": "free", "domain_name": "", "repo": "private", "live": "ask", "note": ""}
KIND_HINT = "launch"

_WEB = re.compile(r"\b(web ?sites?|web ?apps?|landing pages?|waitlist|web ?pages?|homepage|saas|blog|portfolio|"
                  r"online (store|shop)|dashboard|browser game|web game|put (it|this) online|site)\b", re.I)
_MAKE = re.compile(r"\b(build|make|create|develop|code|ship|launch|set up|start|design|put)\b", re.I)


def shippable(prompt: str) -> bool:
    """Whether the goal makes something that will live online (a site or web app)."""
    return bool(_WEB.search(prompt or "") and _MAKE.search(prompt or ""))


def parse(answer: str | None) -> dict[str, str]:
    """The card's answer (JSON), or a typed reply as a note; anything missing takes the default."""
    plan = dict(DEFAULT)
    text = (answer or "").strip()
    try:
        got = json.loads(text) if text.startswith("{") else {"note": text}
    except ValueError:
        got = {"note": text}
    for k in DEFAULT:
        v = str(got.get(k) or "").strip()
        if v:
            plan[k] = v[:200]
    if plan["domain"] not in ("own", "buy", "free"):
        plan["domain"] = "free"
    plan["domain_name"] = re.sub(r"^https?://|/.*$", "", plan["domain_name"].lower())
    plan["repo"] = "public" if plan["repo"] == "public" else "private"
    plan["live"] = "auto" if plan["live"] == "auto" else "ask"
    return plan


def describe(plan: dict[str, str]) -> str:
    where = {"own": f"their own domain {plan['domain_name'] or '(name not given: ask)'}: connect it at their registrar "
                    "(find_integrations for the registrar's DNS steps)",
             "buy": "a new domain" + (f" (they suggested: {plan['domain_name']})" if plan["domain_name"] else "") +
                    ": check a few names' availability and price, let them pick (ask_human with options), then buy it "
                    "(the purchase asks them)",
             "free": "a free address for now (e.g. name.vercel.app); no domain"}[plan["domain"]]
    live = ("put it live as soon as it's ready and checked" if plan["live"] == "auto" else
            "ask them before deploying anything, previews included (Todd asks by itself when an agent deploys or "
            "connects a domain; localhost is always fine)")
    note = f" Their note: {plan['note']}" if plan["note"] else ""
    return (f"Launch plan: address: {where}. Code: a {plan['repo']} GitHub repository. Going live: {live}.{note}")


# ------------------------------------------------------------------------------------------ in a run
def get(ctx: RunContext) -> dict[str, str] | None:
    """The human's launch plan for this run (None until they answer)."""
    plan = getattr(ctx, "launch", None)
    if plan is not None:
        return plan
    it = _answered(ctx.run_id)  # a resumed run: read it back from the answered card
    if it is not None:
        ctx.launch = parse(it.answer)  # type: ignore[attr-defined]
        return ctx.launch  # type: ignore[attr-defined]
    return None


def effective(ctx: RunContext) -> dict[str, str]:
    """The plan, or the defaults while they haven't answered."""
    return get(ctx) or dict(DEFAULT)


def _cards(run_id: str) -> list[Any]:
    from .db import Interaction, select, session

    with session() as s:
        rows = s.exec(select(Interaction).where(Interaction.run_id == run_id,
                                                Interaction.kind == "question")).all()
    return [it for it in rows if (it.data or {}).get("kind_hint") == KIND_HINT]


def _answered(run_id: str) -> Any:
    done = [it for it in _cards(run_id) if it.status == "answered"]
    return done[-1] if done else None


async def ask(ctx: RunContext, what: str = "", agent: str = "planner") -> str:
    """Show the launch card (once per run) without waiting for it; the answer reaches the planner as a message."""
    if get(ctx) is not None:
        return "answered"
    if getattr(ctx, "_launch_task", None) is not None or any(it.status == "pending" for it in _cards(ctx.run_id)):
        return "asked"
    question = (f"How should {what or 'this'} go live?" if what else "Where should this live, and how does it go live?")
    iid = await ctx.open_interaction("question", question, agent, {
        "kind_hint": KIND_HINT, "what": what, "background": True,
        "defaults": DEFAULT,
        "details": "Agents start building meanwhile. Nothing is deployed or made public until you answer (or approve "
                   "it)."})
    ctx._launch_task = asyncio.create_task(_await_answer(ctx, iid), name=f"launch-{ctx.run_id}")  # type: ignore[attr-defined]
    return "asked"


async def _await_answer(ctx: RunContext, iid: str) -> None:
    from .db import Interaction, session
    from .runtime import _pending

    fut = _pending.get(iid)
    try:
        if fut is not None:  # (not wait_interaction: this card doesn't make the run "waiting")
            await asyncio.shield(fut)
    except Exception:  # noqa: BLE001  (cancelled run)
        return
    _pending.pop(iid, None)
    with session() as s:
        it = s.get(Interaction, iid)
    if it is None or it.status != "answered":
        return
    plan = parse(it.answer)
    ctx.launch = plan  # type: ignore[attr-defined]
    text = describe(plan)
    ctx.emit("system", "status", "Launch plan: " + summary(plan), {"launch": plan})
    ctx.user_notes.put_nowait(f"[launch plan from the human] {text}")


def summary(plan: dict[str, str]) -> str:
    """One line for the timeline."""
    where = {"own": f"on {plan['domain_name'] or 'your domain'}", "buy": "on a new domain",
             "free": "on a free address for now"}[plan["domain"]]
    return (f"{where}, {plan['repo']} repository, " +
            ("deploys when ready" if plan["live"] == "auto" else "asks before deploying"))


async def wait(ctx: RunContext, timeout: float | None = None) -> dict[str, str] | None:
    """Wait for the answer (for when the next step depends on it)."""
    task = getattr(ctx, "_launch_task", None)
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            pass
    return get(ctx)


def close(ctx: RunContext) -> None:
    """The run ended: an unanswered launch card has nothing left to decide."""
    from .db import Interaction, session, utcnow
    from .runtime import _pending

    task = getattr(ctx, "_launch_task", None)
    if task is not None and not task.done():
        task.cancel()
    for it in _cards(ctx.run_id):
        if it.status != "pending":
            continue
        with session() as s:
            row = s.get(Interaction, it.id)
            if row is not None and row.status == "pending":
                row.status, row.resolved_at = "cancelled", utcnow()
                s.add(row)
                s.commit()
        fut = _pending.pop(it.id, None)
        if fut is not None and not fut.done():
            fut.set_result(None)
