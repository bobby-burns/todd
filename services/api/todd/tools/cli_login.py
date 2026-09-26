"""Agent side of "sign in once" (see todd/connect.py): connect a service's CLI with the human's browser session."""

from __future__ import annotations

import asyncio

from .. import accounts, connect
from ..runtime import resolve_interaction
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool


@todd_tool(planner=True)
async def cli_login(service: str, reconnect: bool = False) -> dict:
    """Connect a service's CLI with the human's signed-in browser session, so nobody creates or copies a token:
    Todd opens the CLI's sign-in in the shared browser and approves it itself. Only a password or 2FA page needs the
    human, who is asked once. Best done before spawning agents, when find_integrations says route "connect".
    Supported: github (also powers the github toolset, gh and git_push), vercel, netlify, railway, cloudflare,
    stripe, firebase. Afterwards use `cli` (or `gh` for GitHub).

    Args:
        service: e.g. "github", "vercel"
        reconnect: sign in again even if already connected
    """
    c = connect.connector(service)
    if connect.is_connected(c.service) and not reconnect:
        return {"service": c.service, "connected": True, "note": f"Already connected. {_usage(c)}"}
    ctx, agent_id = get_ctx(), get_agent_id()
    try:
        acct = next(iter((await accounts.statuses([c.service]))["accounts"]), None)
    except Exception:  # noqa: BLE001
        acct = None
    if acct and acct["status"] == "signed_out":
        raise ToolError(f"The browser isn't signed in to {acct['name']}. Call request_signins([\"{c.service}\"]) first "
                        "(when the human signs in on the Accounts page, the CLI is connected at the same time).")
    conn = connect.start(c.service, on_change=lambda cn: ctx.emit(agent_id, "status", f"{c.name}: {cn.message}"),
                         browser_lock=ctx.browser_lock)
    asked: str | None = None
    while not conn.done.is_set():
        if conn.state == "needs_you" and asked is None:
            asked = await ctx.open_interaction(
                "question", f"Connecting the {c.name} to your account: {conn.message} Reply here only if you can't.",
                agent_id, {"kind_hint": "signin", "services": [c.service]})
        if asked is not None:
            it = await ctx.wait_interaction(asked, timeout=1)
            if it is not None:  # the human answered instead of finishing in the browser
                if conn.task:
                    conn.task.cancel()
                return {"service": c.service, "connected": False, "note": f"The human replied: {it.answer or ''}"}
        else:
            try:
                await asyncio.wait_for(conn.done.wait(), 1)
            except asyncio.TimeoutError:
                pass
        ctx.check_cancelled()
    if asked is not None:
        resolve_interaction(asked, decision=None, answer="(finished in the browser)")
    if conn.state != "connected":
        raise ToolError(f"Couldn't connect the {c.name}: {conn.message}")
    return {"service": c.service, "connected": True, "note": f"Connected with the human's account. {_usage(c)}"}


def _usage(c: connect.Connector) -> str:
    if c.service == "github":
        return "Use the github toolset, gh(...) and git_push; the token is in the vault as GITHUB_TOKEN."
    return f"Run it with cli(\"{c.service}\", \"{c.example}\")."


CLI_LOGIN_TOOLS = [cli_login]
