"""Pausing an agent also pauses its running browser task (real Chromium over CDP; skipped if unreachable)."""

from __future__ import annotations

import asyncio

import pytest

from todd import cdp
from todd.runtime import RunContext
from todd.sdk import set_ctx, set_current_agent

from .helpers import events_of, new_run

pytestmark = pytest.mark.skipif(not asyncio.run(cdp.online()), reason="browser not reachable over CDP")

PAGE = "http://127.0.0.1:8765/page.html"


def test_pause_freezes_browser_task(loop):
    from browser_use.llm.views import ChatInvokeCompletion
    import browser_use.llm.litellm.chat as blc

    steps = [{"evaluation_previous_goal": "start", "memory": "", "next_goal": f"step {i}",
              "action": [{"navigate": {"url": PAGE}}]} for i in range(4)]
    steps.append({"evaluation_previous_goal": "ok", "memory": "", "next_goal": "done",
                  "action": [{"done": {"text": "finished", "success": True}}]})

    class Slow:
        provider = name = model_name = model = "scripted"

        def __init__(self, **_):
            self.i = 0

        async def ainvoke(self, messages, output_format=None, **_):
            s = steps[min(self.i, len(steps) - 1)]
            self.i += 1
            await asyncio.sleep(0.6)
            return ChatInvokeCompletion(completion=output_format.model_validate(s), usage=None)

    original = blc.ChatLiteLLM
    blc.ChatLiteLLM = Slow

    async def go():
        from todd.agents.browser import run_browser_task

        rid = new_run("browser pause")
        ctx = RunContext(rid)
        set_ctx(ctx)
        set_current_agent("a_browser")
        task = asyncio.create_task(run_browser_task("walk the page"))
        for _ in range(100):
            if any(e.kind == "browser_step" for e in events_of(rid)):
                break
            await asyncio.sleep(0.1)
        ctx.pause_agent("a_browser")
        await asyncio.sleep(2.0)  # the in-flight step may land
        n = len([e for e in events_of(rid) if e.kind == "browser_step"])
        await asyncio.sleep(2.5)
        assert len([e for e in events_of(rid) if e.kind == "browser_step"]) == n, "browser kept going while paused"
        ctx.resume_agent("a_browser")
        res = await asyncio.wait_for(task, 60)
        assert res["success"] and res["result"] == "finished"

    try:
        loop.run_until_complete(go())
    finally:
        blc.ChatLiteLLM = original
