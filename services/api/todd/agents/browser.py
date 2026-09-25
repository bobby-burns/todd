"""Browser automation: browser-use driving the shared Chromium container over CDP (used by the `browse` tool).

The Chromium profile lives on a volume, so the human logs into Google/Vercel/etc. once via the live view
and the agent reuses those sessions. Captchas/2FA -> ask_human, and the human takes over in the live view.
Card details are only provided (as masked placeholders) for tasks with an approved payment.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
from typing import Any

from .. import prompts, vault
from ..config import config
from ..llm import model_for
from ..policy import authorize_spend, settle
from ..sdk import get_agent_id, get_ctx

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")

HUMAN_WAIT_SLICE_S = 150

_CARD_FIELDS = {
    "card_number": "CARD_NUMBER",
    "card_exp": "CARD_EXP",
    "card_cvc": "CARD_CVC",
    "card_name": "CARD_NAME",
    "card_zip": "CARD_ZIP",
}


def cdp_url() -> str:
    host = config.browser_cdp_host
    try:
        # Chrome rejects CDP requests whose Host header is a hostname other than localhost; use the IP.
        host = socket.gethostbyname(host)
    except OSError:
        pass
    return f"http://{host}:{config.browser_cdp_port}"


def _card_secrets(domains: list[str]) -> dict[str, dict[str, str]] | None:
    card = {k: vault.get_secret(v) for k, v in _CARD_FIELDS.items()}
    card = {k: v for k, v in card.items() if v}
    if "card_number" not in card:
        return None
    out: dict[str, dict[str, str]] = {}
    for d in domains:
        d = d.strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
        if d:
            out[f"https://{d}"] = card
            out[f"https://*.{d}"] = card
    return out or None


async def run_browser_task(task: str, start_url: str | None = None, payment: dict[str, Any] | None = None) -> dict:
    ctx = get_ctx()
    agent_id = get_agent_id()
    from browser_use import Agent, Browser, Tools
    from browser_use.agent.views import ActionResult
    from browser_use.llm.litellm.chat import ChatLiteLLM

    spec = model_for("browser")
    llm = ChatLiteLLM(model=spec.model, api_key=spec.api_key, api_base=spec.api_base)

    sensitive: dict[str, Any] | None = None
    ledger = None
    extra = ""
    if payment:
        domains = payment.get("domains") or []
        sensitive = _card_secrets(domains)
        if sensitive is None:
            return {"success": False, "result": "No payment card is stored in the vault (Settings → Payment card)."}
        ledger = await authorize_spend(
            ctx, amount_usd=float(payment["amount_usd"]), merchant=payment.get("merchant", domains[0]),
            description=payment.get("description", task[:200]) + f" (card usable only on: {', '.join(domains)})",
            method="card", agent=agent_id, data={"domains": domains}, require_human=True,
        )
        extra = (
            f"\nA payment of at most ${float(payment['amount_usd']):.2f} to {payment.get('merchant')} is APPROVED for "
            "this task. Card fields are available as secrets: card_number, card_exp (MM/YY), card_cvc, card_name, "
            "card_zip. Do not pay more than the approved amount; if the total differs, stop and ask_human."
        )

    tools = Tools()

    async def _await_human(iid: str, question: str) -> ActionResult:
        # browser-use caps each action at ~180s, so wait in bounded slices; the question stays open between calls.
        it = await ctx.wait_interaction(iid, timeout=HUMAN_WAIT_SLICE_S)
        if it is None:
            return ActionResult(
                extracted_content=f"The human hasn't answered yet (question_id={iid}). They may be using this "
                                  f"browser right now: don't interact with the page. Call wait_for_human with "
                                  f"question_id={iid} to keep waiting.",
                long_term_memory=f"Waiting for human on question_id={iid}: {question}")
        answer = it.answer or ""
        ctx.emit(agent_id, "human_reply", answer or "(no answer)", {"interaction_id": iid})
        return ActionResult(extracted_content=f"Human replied: {answer}",
                            long_term_memory=f"Asked human: {question} -> {answer}")

    @tools.action("Ask the human operator for help (login, captcha, 2FA code, a decision). They can see and "
                  "control this browser in the Live Browser panel. Returns their reply, or a question_id to keep "
                  "waiting on with wait_for_human.")
    async def ask_human(question: str) -> ActionResult:
        iid = await ctx.open_interaction("question", question, agent_id)
        return await _await_human(iid, question)

    @tools.action("Keep waiting for the human's answer to a question you already asked (pass its question_id).")
    async def wait_for_human(question_id: str) -> ActionResult:
        return await _await_human(question_id, "(continued)")

    card_mode = sensitive is not None  # never screenshot or send images while card details may be on screen
    shots_dir = config.data_dir / "screens" / ctx.run_id
    shots_dir.mkdir(parents=True, exist_ok=True)

    async def on_step(state: Any, output: Any, step: int) -> None:
        data: dict[str, Any] = {"step": step, "url": getattr(state, "url", None), "title": getattr(state, "title", None)}
        try:
            data["actions"] = [a.model_dump(exclude_unset=True, exclude_none=True) for a in (output.action or [])]
        except Exception:
            pass
        data["memory"] = getattr(output, "memory", None)
        data["evaluation"] = getattr(output, "evaluation_previous_goal", None)
        if getattr(output, "thinking", None):
            ctx.emit(agent_id, "thinking", str(output.thinking), {"source": "browser"})
        shot = getattr(state, "screenshot", None)
        if shot and not card_mode:
            name = f"{step:04d}.png"
            try:
                (shots_dir / name).write_bytes(base64.b64decode(shot))
                data["screenshot"] = f"/api/screens/{ctx.run_id}/{name}"
            except Exception:
                pass
        ctx.emit(agent_id, "browser_step", getattr(output, "next_goal", None) or f"Step {step}", data)

    async def should_stop() -> bool:
        return ctx.cancelled

    full_task = task + (f"\nStart at: {start_url}" if start_url else "") + extra
    if ctx.browser_lock.locked():
        ctx.emit(agent_id, "status", "Waiting for the browser (another agent is using it)…")
    async with ctx.browser_lock:
        browser = Browser(cdp_url=cdp_url(), keep_alive=True)
        agent = Agent(
            task=full_task,
            llm=llm,
            browser=browser,
            tools=tools,
            sensitive_data=sensitive,
            extend_system_message=prompts.compose("browser"),
            register_new_step_callback=on_step,
            register_should_stop_callback=should_stop,
            use_vision=not card_mode,
            use_judge=False,
        )
        ctx.emit(agent_id, "status", f"Browser task started: {task[:200]}", {"browser": "start"})

        async def pause_bridge() -> None:
            # Mirror Todd's pause switch onto browser-use's own pause (which waits between steps, outside the
            # per-step timeout), so pausing an agent also pauses its browser task.
            gate = ctx.gate(agent_id)
            while True:
                await gate.paused.wait()
                agent.pause()
                await gate.running.wait()
                agent.resume()

        bridge = asyncio.create_task(pause_bridge())
        try:
            history = await agent.run(max_steps=config.browser_max_steps)
        except BaseException as e:
            if ledger is not None:
                settle(ledger, "needs_review", {"error": f"{type(e).__name__}: {e}"[:500]})
            raise
        finally:
            bridge.cancel()
            try:
                await browser.stop()
            except Exception:
                pass

    try:
        cost = float(getattr(getattr(history, "usage", None), "total_cost", 0) or 0)
        ctx.add_llm_cost(cost)
    except Exception:
        pass

    success = bool(history.is_successful())
    result = history.final_result() or ""
    errors = [e for e in history.errors() if e][-3:]
    if ledger is not None:
        settle(ledger, "completed" if success else "needs_review", {"result": result[:500]})
    ctx.emit(agent_id, "status", ("Browser task finished" if success else "Browser task stopped")
             + f" after {len(history.history)} steps", {"browser": "end"})
    return {"success": success, "result": result, "errors": errors, "steps": len(history.history)}
