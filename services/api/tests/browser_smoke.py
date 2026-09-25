"""Browser smoke test: drives the real browser container through run_browser_task with a scripted model
(no API key needed). Checks CDP connectivity, step events, screenshots and the ask_human pause.

    docker compose exec api python tests/browser_smoke.py
"""

import asyncio
import os

from browser_use.llm.views import ChatInvokeCompletion
import browser_use.llm.litellm.chat as blc

from todd.db import Event, Interaction, Run, init_db, select, session
from todd.runtime import RunContext, resolve_interaction
from todd.sdk import set_ctx

URL = os.getenv("SMOKE_URL", "https://example.com")
SCRIPT = [
    {"evaluation_previous_goal": "start", "memory": "", "next_goal": f"Open {URL}", "action": [{"navigate": {"url": URL}}]},
    {"evaluation_previous_goal": "ok", "memory": "", "next_goal": "Check with the human",
     "action": [{"ask_human": {"question": "Smoke test: reply anything"}}]},
    {"evaluation_previous_goal": "ok", "memory": "", "next_goal": "Done",
     "action": [{"done": {"text": "smoke ok", "success": True}}]},
]


class ScriptedBrowserLLM:
    provider = name = model_name = model = "scripted"

    def __init__(self, **_):
        self.i = 0

    async def ainvoke(self, messages, output_format=None, **_):
        step = SCRIPT[min(self.i, len(SCRIPT) - 1)]
        self.i += 1
        return ChatInvokeCompletion(completion=output_format.model_validate(step), usage=None)


blc.ChatLiteLLM = ScriptedBrowserLLM


async def main():
    init_db()
    with session() as s:
        run = Run(title="browser smoke", prompt="smoke", budget_usd=0, status="running")
        s.add(run)
        s.commit()
        rid = run.id
    set_ctx(RunContext(rid))
    from todd.agents.browser import run_browser_task

    async def answer():
        while True:
            await asyncio.sleep(0.3)
            with session() as s:
                it = s.exec(select(Interaction).where(Interaction.run_id == rid, Interaction.status == "pending")).first()
            if it:
                resolve_interaction(it.id, decision=None, answer="ok")
                return

    answering = asyncio.create_task(answer())
    res = await asyncio.wait_for(run_browser_task("smoke test"), 180)
    await answering
    with session() as s:
        shots = [e for e in s.exec(select(Event).where(Event.run_id == rid)).all() if e.data.get("screenshot")]
    print("result:", res)
    print("screenshots:", len(shots))
    assert res["success"], res
    print("BROWSER SMOKE OK — run", rid)


asyncio.run(main())
