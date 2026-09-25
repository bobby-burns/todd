"""Human-in-the-loop tools every agent (and the planner) gets."""

from __future__ import annotations

from ..sdk import get_agent_id, get_ctx, todd_tool


@todd_tool
async def ask_human(question: str) -> str:
    """Ask the human operator one precise question and wait for their answer. Use only when truly blocked
    (a decision only they can make, a login/captcha/2FA, missing information).

    Args:
        question: the question, with the context they need to answer quickly
    """
    return await get_ctx().ask_human(question, agent=get_agent_id())


@todd_tool
async def request_approval(action: str, details: str = "") -> dict:
    """Ask the human to approve an irreversible, risky or public action that doesn't cost money: posting or
    messaging from their accounts (include the exact text), emailing people, deleting data, publishing. Spending
    is handled by the spend tools automatically — don't use this for purchases.

    Args:
        action: one-line description of what you want to do
        details: everything the human needs to decide, e.g. the exact post text and where it goes
    """
    approved, note = await get_ctx().request_approval(action, agent=get_agent_id(), data={"details": details})
    return {"approved": approved, "note": note}


HUMAN_TOOLS = [ask_human, request_approval]
