"""Example Todd plugin: push a notification to your phone via ntfy.sh.

Any .py file in ./plugins is loaded at startup (or via Settings → Tools & plugins → Reload).
Every LangChain tool at module level is registered into a toolset (default: the file name) that the planner
can hand to agents it spawns. `planner=True` also gives the tool to the planner directly.

Setup: install the ntfy app, subscribe to a hard-to-guess topic, then add the topic name to the vault as
NTFY_TOPIC (Settings → Vault). Delete this file if you don't want it.
"""

import httpx

from todd.sdk import ToolError, get_ctx, get_secret, todd_tool


@todd_tool(toolset="notify", planner=True)
async def notify_me(message: str, priority: str = "default") -> str:
    """Send a push notification to the human's phone. Use when a long task finishes or when you need them
    to look at something (e.g. a pending approval).

    Args:
        message: short notification text
        priority: one of min, low, default, high, urgent
    """
    topic = get_secret("NTFY_TOPIC")
    if not topic:
        raise ToolError("NTFY_TOPIC is not set in the vault; skip notifications.")
    ctx = get_ctx()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"https://ntfy.sh/{topic}", content=message.encode(),
                         headers={"Title": f"Todd run {ctx.run_id}", "Priority": priority})
    r.raise_for_status()
    return "notification sent"
