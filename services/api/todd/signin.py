"""Sign in once, up-front: the account check that runs when a goal arrives, and the sign-in card it shows.

`preflight` runs before the planner of a new run: it reads which accounts the goal needs (needs.detect), checks the
browser, and if any are missing pauses the run with one card: a Sign in button per service (opens its login page in
the live browser) and Later. The card closes itself once every service is signed in. Afterwards the CLIs of the
signed-in services are connected with that session (connect.py), so keys and CLIs are in place before any agent
starts. `gate` is the same card for the planner's request_signins.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from . import accounts, connect, needs
from .runtime import RunCancelled, RunContext, resolve_interaction

log = logging.getLogger("todd.signin")

POLL = 2.0  # seconds between status checks while the card is open
PROBE_EVERY = 8.0  # seconds between page probes for services whose cookies don't tell
VERIFY_MAX = 6  # at most this many page probes at once


def _probeable(row: dict[str, Any]) -> bool:
    """Not signed in yet, and only a visit can tell: no well-known session cookie decided it, and the site has cookies
    (none at all means signed out for sure)."""
    return row["status"] != "signed_in" and row.get("source") != "session cookie" and bool(row.get("cookie_count", 1))


async def _status(ids: list[str], probe: bool) -> dict[str, str]:
    st = await accounts.statuses(ids)
    if not st["browser_online"]:
        return {i: "offline" for i in ids}
    if probe:
        todo = [a["id"] for a in st["accounts"] if _probeable(a)][:VERIFY_MAX]
        if todo:
            await asyncio.gather(*(asyncio.wait_for(accounts.verify(i), 25) for i in todo), return_exceptions=True)
            st = await accounts.statuses(ids)
    return {a["id"]: a["status"] for a in st["accounts"]}


def _card_text(missing: list[dict[str, str]], reason: str) -> str:
    names = ", ".join(s["name"] for s in missing)
    head = f"Sign in to {names} so Todd can work without stopping later."
    return head + (f"\nWhy: {reason}" if reason else "") + \
        "\nTap Sign in: the login page opens in Todd's browser. This card closes by itself when you're done."


async def gate(ctx: RunContext, services: list[dict[str, str]],
               reason: str | Callable[[list[dict[str, str]]], str] = "", agent: str = "planner",
               timeout: float | None = None) -> dict[str, Any]:
    """Show the sign-in card for the services that aren't signed in and wait until they are, or until the human
    taps Continue / Later. `reason` can be a function of the services the card lists (the ones not signed in yet).
    Returns {"signed_in": [...], "later": [...], "offline": bool, "asked": bool}."""
    ids = [s["id"] for s in services]
    if not ids:
        return {"signed_in": [], "later": [], "offline": False, "asked": False}
    rows = await _status(ids, probe=True)
    if all(v == "offline" for v in rows.values()):
        return {"signed_in": [], "later": ids, "offline": True, "asked": False}
    missing = [s for s in services if rows.get(s["id"]) != "signed_in"]
    if not missing:
        return {"signed_in": ids, "later": [], "offline": False, "asked": False}
    if callable(reason):
        reason = reason(missing)
    data = {"kind_hint": "signin",
            "services": [{**s, "status": rows.get(s["id"], "unknown")} for s in missing],
            "reason": reason}
    it_id = await ctx.open_interaction("approval", _card_text(missing, reason), agent, data)
    loop = asyncio.get_running_loop()
    start = last_probe = loop.time()
    decided = None
    try:
        while True:
            it = await ctx.wait_interaction(it_id, timeout=POLL)
            if it is not None:
                decided = it.status  # approved = Continue, denied = Later
                break
            ctx.check_cancelled()
            probe = loop.time() - last_probe >= PROBE_EVERY  # sites without a known session cookie need a visit
            if probe:
                last_probe = loop.time()
            now = await _status([s["id"] for s in missing], probe=probe)
            if all(v == "signed_in" for v in now.values()):
                resolve_interaction(it_id, decision="approve", answer="(signed in)")
                decided = "approved"
                break
            if timeout is not None and loop.time() - start > timeout:
                break
    finally:
        if decided is None:  # timed out, failed or cancelled: don't leave the run waiting on a dead card
            resolve_interaction(it_id, decision="deny", answer="(closed)")
    final = await _status(ids, probe=decided == "approved")
    signed = [i for i in ids if final.get(i) == "signed_in"]
    return {"signed_in": signed, "later": [i for i in ids if i not in signed], "offline": False, "asked": True,
            "choice": "later" if decided == "denied" else "continue"}


async def connect_clis(ctx: RunContext, ids: list[str]) -> list[str]:
    """Sign in the CLIs of signed-in services with that session, one at a time (they share the browser)."""
    from .tools.cli_login import cli_login

    done = []
    for sid in ids:
        if sid not in connect.CONNECTORS or connect.is_connected(sid):
            continue
        name = connect.CONNECTORS[sid].name
        ctx.emit("system", "status", f"Connecting the {name} with your {accounts.catalog()[sid].name} sign-in")
        try:
            r = await cli_login.ainvoke({"service": sid})
            if r.get("connected"):
                done.append(sid)
        except RunCancelled:
            raise
        except Exception as e:  # noqa: BLE001  (the planner sees it's not connected and can retry)
            ctx.emit("system", "error", f"Couldn't connect the {name}: {e}")
    return done


async def preflight(ctx: RunContext, prompt: str) -> str:
    """The up-front account check for a new run. Returns a note for the planner's system prompt."""
    try:
        wanted = needs.detect(prompt)
    except Exception:  # noqa: BLE001
        log.exception("account detection failed")
        return ""
    if not wanted:
        return ""
    names = ", ".join(s["name"] for s in wanted)
    ctx.emit("system", "status", f"Checking the accounts this goal needs: {names}",
             {"preflight": [s["id"] for s in wanted]})
    try:
        res = await gate(ctx, wanted, reason=needs.explain, agent="planner")
    except RunCancelled:
        raise
    except Exception:  # noqa: BLE001  (never block a run on the check itself)
        log.exception("sign-in check failed")
        return ""
    if res["offline"]:
        ctx.emit("system", "status", "The browser is offline, so accounts weren't checked")
        return "- Up-front account check: skipped (browser offline). Use check_accounts before browser work."
    connected = await connect_clis(ctx, res["signed_in"])
    cat = accounts.catalog()
    label = lambda ids: ", ".join(cat[i].name for i in ids) or "none"  # noqa: E731
    lines = [f"- Up-front account check (done before you started; don't repeat it): signed in: "
             f"{label(res['signed_in'])}."]
    if connected:
        lines.append(f"- CLIs connected with those sign-ins just now: {label(connected)}.")
    from . import integrations

    for it in integrations.setup_pending(res["signed_in"]):
        lines.append(f"- {it.name} is signed in but its keys aren't in the vault yet: set them up first "
                     f"(find_integrations([\"{it.name}\"]) → route \"setup\"), before spawning the other agents.")
    if res["later"]:
        how = "chose Later for" if res.get("choice") == "later" else "isn't signed in to"
        lines.append(f"- The human {how}: {label(res['later'])}. Plan around them; if one becomes essential, call "
                     "request_signins for it at that point (it shows the same Sign in / Later card).")
    return "\n".join(lines)
