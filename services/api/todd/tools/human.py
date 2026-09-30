"""Human-in-the-loop tools every agent (and the planner) gets."""

from __future__ import annotations

from ..sdk import get_agent_id, get_ctx, todd_tool


@todd_tool
async def ask_human(question: str, secret_name: str | None = None, options: list[str] | None = None) -> str:
    """Ask the human operator one precise question and wait for their answer. Use only when truly blocked
    (a decision only they can make, a login/captcha/2FA, missing information).

    When the answer is a choice, pass `options` (2–6 short choices, e.g. domain names with their prices): they tap
    one instead of typing, and can still write something else.

    To collect a password, API key or other secret, pass secret_name (UPPER_SNAKE_CASE): the human gets a password
    field, the answer goes straight into the vault under that name, and you only learn that it was saved (then use
    {{secret:NAME}}). Never ask for a secret without it.

    Args:
        question: the question, with the context they need to answer quickly
        secret_name: vault name for a secret answer, e.g. "DATABASE_PASSWORD"
        options: choices to tap, e.g. ["quizdaily.com ($12/yr)", "dailyml.dev ($15/yr)"]
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
    choices = [str(o).strip()[:120] for o in (options or []) if str(o).strip()][:8]
    return await get_ctx().ask_human(question, agent=get_agent_id(), data={"options": choices} if choices else None)


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


@todd_tool
async def plan_launch(what: str = "", wait: bool = False) -> str:
    """The human's launch plan for something that will be online: where it lives (their own domain, a new domain,
    or a free address), whether the GitHub repository is private or public, and whether to ask them before it goes
    live. For a website goal Todd shows this card when the run starts; call this to read the answer, or to ask if
    nobody has yet. Don't hold up building for it: it's needed at the repository, deploy and domain steps.

    Args:
        what: what's being launched, in a few words (e.g. "your AI quiz site"), used on the card if it's asked now
        wait: wait for the answer (use when your next step depends on it)
    """
    from .. import launch

    ctx = get_ctx()
    if launch.get(ctx) is None:
        await launch.ask(ctx, what, agent=get_agent_id())
        if wait:
            await launch.wait(ctx)
    plan = launch.get(ctx)
    if plan is None:
        return ("Asked the human (a card with the address, private/public repo and going-live choices). Their answer "
                "arrives as a message. Until then: private repository, nothing live; previews and localhost are fine.")
    return launch.describe(plan)


@todd_tool
async def go_live(what: str, where: str) -> str:
    """Before making something live for everyone: a production deploy, connecting a domain, publishing a site.
    Unless the human's launch plan says to put it live when ready, they're asked once for this run (production
    deploys through the CLIs and APIs ask by themselves; call this first in the browser, or to ask up front).

    Args:
        what: what goes live, e.g. "the AI quiz site"
        where: the address, e.g. "https://quizdaily.com" or "the Vercel production URL"
    """
    from ..gates import approve_live

    await approve_live(get_ctx(), get_agent_id(), f"{what} at {where}", f"{what}\nAddress: {where}")
    return f"Approved: {what} can go live at {where} (for the rest of this run)."


HUMAN_TOOLS = [ask_human, request_approval, authorize_purchase, plan_launch, go_live]
