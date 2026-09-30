"""Human-in-the-loop tools every agent (and the planner) gets."""

from __future__ import annotations

from ..sdk import get_agent_id, get_ctx, todd_tool


@todd_tool
async def ask_human(question: str, secret_name: str | None = None) -> str:
    """Ask the human operator one precise question and wait for their answer. Use only when truly blocked
    (a decision only they can make, a login/captcha/2FA, missing information).

    To collect a password, API key or other secret, pass secret_name (UPPER_SNAKE_CASE): the human gets a password
    field, the answer goes straight into the vault under that name, and you only learn that it was saved (then use
    {{secret:NAME}}). Never ask for a secret without it.

    Args:
        question: the question, with the context they need to answer quickly
        secret_name: vault name for a secret answer, e.g. "DATABASE_PASSWORD"
    """
    data = None
    if secret_name:
        from .. import vault
        from .page_capture import check_name

        check_name(secret_name)
        data = {"secret_name": secret_name}
        answer = await get_ctx().ask_human(question, agent=get_agent_id(), data=data)
        if not vault.has_secret(secret_name):
            return f"The human didn't provide {secret_name}. ({answer or 'no answer'})"
        return f"Saved to the vault as {secret_name}. Reference it as {{{{secret:{secret_name}}}}}."
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
