"""Plugin used by the test suite (also a minimal example of the plugin API)."""

from todd.sdk import authorize_spend, get_ctx, settle, todd_tool


@todd_tool(toolset="demo", planner=True)
async def echo(text: str) -> str:
    """Echo text back.

    Args:
        text: text to echo
    """
    return f"echo: {text}"


@todd_tool(toolset="demo", planner=True)
async def fake_purchase(amount_usd: float, item: str) -> str:
    """Pretend to buy something (exercises the spend policy).

    Args:
        amount_usd: price
        item: what to buy
    """
    entry = await authorize_spend(get_ctx(), amount_usd=amount_usd, merchant="Test Shop", description=f"Buy {item}")
    settle(entry, "completed")
    return f"purchased {item}"


@todd_tool(toolset="demo")
async def shout(text: str) -> str:
    """Uppercase text (a custom tool routed to the code agent).

    Args:
        text: text
    """
    return text.upper()
