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
async def request_approval(action: str, details: str = "", sites: list[str] | None = None) -> dict:
    """Ask the human to approve an irreversible, risky or public action that doesn't cost money: posting or
    messaging from their accounts (include the exact text), emailing people, deleting data, publishing. Spending
    is handled by the spend tools automatically — don't use this for purchases.

    In the browser, sites where the human's account talks to people (x.com, LinkedIn, Reddit, Gmail…) are locked
    until they approve: pass sites=["x.com"] and, once approved, you can act there for 30 minutes (for this only).

    Args:
        action: one-line description of what you want to do
        details: everything the human needs to decide, e.g. the exact post text and where it goes
        sites: websites this approval opens for you in the browser, e.g. ["x.com"]
    """
    from ..gates import approve_public

    return await approve_public(get_ctx(), get_agent_id(), action, details, sites)


@todd_tool
async def authorize_purchase(amount_usd: float, merchant: str, description: str, sites: list[str]) -> str:
    """Get a purchase approved before making it where Todd can't see the price: clicking Buy / Upgrade / Subscribe /
    Pay on a site where a card may already be saved (plans, credits, seats, a paid tier), or an API/CLI call that
    buys something (a domain through a registrar's API). Todd holds those back until this is approved; the human
    approves it and it goes in the Ledger. Then you have 30 minutes, for this purchase only. (A new card checkout is
    the browser's payment_* arguments instead; a Vercel domain is vercel_buy_domain.)

    Args:
        amount_usd: the total you expect to pay, including tax (0 if you're sure it's free: the human confirms)
        merchant: who gets paid, e.g. "Vercel"
        description: what's being bought, e.g. "Vercel Pro plan, 1 seat, monthly"
        sites: the service's site, e.g. ["vercel.com"] (its subdomains and API are included)
    """
    from ..gates import approve_purchase

    return await approve_purchase(get_ctx(), get_agent_id(), amount_usd, merchant, description, sites)


HUMAN_TOOLS = [ask_human, request_approval, authorize_purchase]
