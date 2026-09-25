"""The `browser` toolset: a real Chromium (the human's signed-in sessions) driven by browser-use."""

from __future__ import annotations

import re

from ..sdk import todd_tool


@todd_tool(toolset="browser")
async def browse(task: str, why_not_api: str, start_url: str | None = None, payment_amount_usd: float | None = None,
                 payment_merchant: str | None = None, payment_domains: list[str] | None = None) -> dict:
    """LAST RESORT: run a focused, multi-step task in the real browser, where the human is signed in to their
    accounts. Only use it when find_integrations shows no usable API, MCP server or CLI for this job (e.g. a
    console with no API, an app-reviewed posting API, a one-off checkout). Give one clear goal, the "done" condition, and exactly which values to report back. The human
    can watch and take over (captchas, 2FA). Only set payment_* when the task must pay by card: it always needs
    the human's approval, and card details only work on payment_domains.

    Args:
        task: what to do and what to report back
        why_not_api: one sentence on why no API/MCP/CLI route works for this (shown to the human)
        start_url: optional URL to start at
        payment_amount_usd: maximum card payment for this task, if any
        payment_merchant: who is being paid
        payment_domains: exact domains where card details may be entered, e.g. ["namecheap.com", "checkout.stripe.com"]
    """
    from ..agents.browser import run_browser_task
    from ..sdk import get_agent_id, get_ctx

    if len((why_not_api or "").strip()) < 10:
        return {"success": False, "result": "Explain in why_not_api why no API, MCP server or CLI works here "
                                            "(call find_integrations first)."}
    get_ctx().emit(get_agent_id(), "status", f"Using the browser: {why_not_api.strip()[:300]}", {"browser_reason": True})

    payment = None
    if payment_amount_usd is not None:
        domains = [clean_domain(d) for d in (payment_domains or [])]
        if not domains or any(d is None for d in domains):
            return {"success": False, "result": "payment_domains must be a list of exact site domains like "
                                                "['namecheap.com', 'checkout.stripe.com'] (no wildcards)."}
        payment = {"amount_usd": payment_amount_usd, "merchant": payment_merchant or domains[0],
                   "domains": domains, "description": task[:300]}
    return await run_browser_task(task, start_url=start_url, payment=payment)


_DOMAIN = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


def clean_domain(raw: str) -> str | None:
    d = raw.strip().lower()
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    d = d.split("/")[0].split(":")[0].strip(".")
    if "*" in d or not _DOMAIN.fullmatch(d):
        return None
    return d


BROWSER_TOOLS = [browse]
