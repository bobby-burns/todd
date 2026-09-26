"""Pause/resume, responsive planner (messages end waits, resume paused agents), end-of-work summaries,
API-first routing, task checklists."""

from __future__ import annotations

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage

from todd import vault
from todd.db import AgentInstance, Event, select, session
from todd.orchestrator import manager
from todd.runtime import resolve_interaction
from todd.sdk import ToolError

from .helpers import SCRIPTS, agents_of, call, events_of, new_run, pending, wait_status


def slow(msg: AIMessage, secs: float = 0.4):
    def f(msgs):
        time.sleep(secs)
        return msg
    return f


async def until(pred, timeout: float = 10) -> None:
    for _ in range(int(timeout * 20)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_pause_and_resume_agent(loop):
    async def go():
        SCRIPTS["Pausable"] = [
            slow(AIMessage("step one", tool_calls=[call("echo", text="one")])),
            slow(AIMessage("step two", tool_calls=[call("echo", text="two")])),
            slow(AIMessage("step three", tool_calls=[call("echo", text="three")])),
            AIMessage("", tool_calls=[call("finish", success=True, summary="Done: three echoes\nOutputs: none\nHow: demo\nLeft / needs you: nothing")]),
        ]
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("spawn_agent", name="Pausable", instructions="x", task="echo thrice",
                                           toolsets=["demo"], background=True)]),
            lambda msgs: AIMessage("", tool_calls=[call("wait_for_agents")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary="ok")]),
        ]
        rid = new_run("pause test")
        manager.start(rid)
        for _ in range(100):
            ags = agents_of(rid)
            if ags and any(e.kind == "tool_call" for e in events_of(rid) if e.agent == ags[0].id):
                break
            await asyncio.sleep(0.05)
        aid = agents_of(rid)[0].id
        assert manager.pause(rid, aid) == 1
        assert manager.pause(rid, aid) == 0  # already paused
        assert agents_of(rid)[0].status == "paused"
        await asyncio.sleep(1.2)  # let any in-flight step land
        n = len([e for e in events_of(rid) if e.agent == aid and e.kind == "tool_call"])
        await asyncio.sleep(1.2)
        assert len([e for e in events_of(rid) if e.agent == aid and e.kind == "tool_call"]) == n  # frozen
        assert manager.pause(rid, aid, resume=True) == 1
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded"
        texts = [e.text for e in events_of(rid) if e.agent == aid and e.kind == "status"]
        assert any("Paused" in t for t in texts) and any("Resumed" in t for t in texts)
        assert agents_of(rid)[0].status == "succeeded"
    loop.run_until_complete(go())


def test_pause_all_includes_planner(loop):
    async def go():
        SCRIPTS["planner"][:] = [slow(AIMessage("thinking", tool_calls=[call("echo", text="a")]), 0.3),
                                 slow(AIMessage("", tool_calls=[call("finish", success=True, summary="ok")]), 0.3)]
        rid = new_run("pause planner")
        manager.start(rid)
        await asyncio.sleep(0.1)
        assert manager.pause(rid) >= 1 and manager.is_paused(rid, "planner")
        await asyncio.sleep(1.0)
        assert manager.is_active(rid)  # held at the gate
        manager.pause(rid, resume=True)
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded"
    loop.run_until_complete(go())


def test_summary_events_and_fallback(loop):
    async def go():
        SCRIPTS["Finisher"] = [AIMessage("", tool_calls=[call("finish", success=True, summary="Done: it\nOutputs: x")])]
        SCRIPTS["Quitter"] = [slow(AIMessage("Starting the long part.", tool_calls=[call("echo", text="hi")]), 0.2),
                              AIMessage("", tool_calls=[call("ask_human", question="wait here")])]
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[
                call("spawn_agent", name="Finisher", instructions="x", task="y", toolsets=[], background=True),
                call("spawn_agent", name="Quitter", instructions="x", task="y", toolsets=["demo"], background=True)]),
            lambda msgs: AIMessage("", tool_calls=[call("wait_for_agents")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary="Done: all\nAgents: 2")]),
        ]
        rid = new_run("summaries")
        manager.start(rid)
        await pending(rid)
        quitter = [a for a in agents_of(rid) if a.name == "Quitter"][0]
        manager.cancel_agent(rid, quitter.id)
        await wait_status(rid, {"succeeded", "failed"})
        summaries = {e.agent: e.text for e in events_of(rid) if e.kind == "summary"}
        fin = [a for a in agents_of(rid) if a.name == "Finisher"][0]
        assert summaries[fin.id].startswith("Done: it")
        assert "Stopped before finishing" in summaries[quitter.id] and "echo" in summaries[quitter.id]
        assert "Starting the long part" in summaries[quitter.id]
        assert summaries["planner"].startswith("Done: all")
    loop.run_until_complete(go())


def test_spawn_tasks_checklist(loop):
    async def go():
        SCRIPTS["Grouped"] = [AIMessage("", tool_calls=[call("finish", success=True, summary="ok")])]
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("spawn_agent", name="Grouped", instructions="x", task="Store listing work",
                                           toolsets=[], tasks=["keywords", "metadata", "submit"])]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary="ok")]),
        ]
        rid = new_run("checklist")
        manager.start(rid)
        await wait_status(rid, {"succeeded", "failed"})
        (a,) = agents_of(rid)
        assert "Checklist" in a.task and "1. keywords" in a.task and "3. submit" in a.task
    loop.run_until_complete(go())


def test_find_integrations_routes(loop):
    from todd import integrations, settings

    vault.set_secret("VERCEL_TOKEN", "vc_test_123456789")
    vault.set_secret("STRIPE_SECRET_KEY", "sk_test_123456789")
    vault.delete_secret("LINEAR_API_KEY")
    settings.update({"mcp_servers": {}})
    ts = {"vercel", "web", "sandbox"}
    r = {x["service"]: x for x in [integrations.assess(s, ts) for s in ["vercel", "stripe", "linear", "tiktok", "foo"]]}
    assert r["Vercel"]["route"] == "toolset"
    assert r["Stripe"]["route"] == "api" and "{{secret:STRIPE_SECRET_KEY}}" in r["Stripe"]["recommendation"]
    assert r["Linear"]["route"] == "mcp_available" and "mcp.linear.app" in r["Linear"]["recommendation"]
    assert r["TikTok"]["route"] == "browser"
    assert r["foo"]["known"] is False
    assert integrations.assess("porkbun", ts | {"porkbun"})["route"] == "toolset"  # plugin toolset
    settings.update({"mcp_servers": {"linear": {"transport": "streamable_http", "url": "https://mcp.linear.app/mcp"}}})
    assert integrations.assess("linear", ts | {"mcp_linear"})["route"] == "mcp"
    settings.update({"mcp_servers": {}})


def test_api_request_secret_binding():
    from todd.tools.web import _inject

    vault.set_secret("GITHUB_TOKEN", "ghp_bindingtest12345")
    vault.set_secret("STRIPE_SECRET_KEY", "sk_test_123456789")
    assert _inject("Bearer {{secret:GITHUB_TOKEN}}", "api.github.com") == "Bearer ghp_bindingtest12345"
    with pytest.raises(ToolError):
        _inject("Bearer {{secret:GITHUB_TOKEN}}", "evil.example.com")
    with pytest.raises(ToolError):
        _inject("{{secret:ANTHROPIC_API_KEY}}", "api.anthropic.com")  # model keys never leave
    assert _inject("{{secret:STRIPE_SECRET_KEY}}", "api.stripe.com") == "sk_test_123456789"


def test_browse_requires_reason(loop):
    from todd.tools.browser_tools import browse

    r = loop.run_until_complete(browse.ainvoke({"task": "do a thing", "why_not_api": "no"}))
    assert r["success"] is False and "why_not_api" in r["result"]


def test_message_interrupts_wait_so_planner_can_spawn_more(loop):
    async def go():
        seen: dict[str, str] = {}

        def react(msgs):
            seen["wait"], seen["note"] = str(msgs[-2].content), str(msgs[-1].content)
            return AIMessage("", tool_calls=[call("spawn_agent", name="Docs", instructions="x", task="write docs",
                                                  toolsets=[])])

        SCRIPTS["Long"] = [AIMessage("", tool_calls=[call("ask_human", question="Keep going?")]),
                           AIMessage("", tool_calls=[call("finish", success=True, summary="long done")])]
        SCRIPTS["Docs"] = [AIMessage("", tool_calls=[call("finish", success=True, summary="docs done")])]
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("spawn_agent", name="Long", instructions="x", task="long job", toolsets=[])]),
            lambda msgs: AIMessage("", tool_calls=[call("wait_for_agents")]),
            react,
            lambda msgs: AIMessage("", tool_calls=[call("wait_for_agents")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary=str(msgs[-1].content))]),
        ]
        rid = new_run("interrupt the wait")
        manager.start(rid)
        it = await pending(rid)  # Long is asking the human; the planner is waiting on it
        await until(lambda: any(e.agent == "planner" and e.kind == "tool_call" and e.data.get("tool") == "wait_for_agents"
                                for e in events_of(rid)))
        assert manager.note(rid, "Also write the docs.")
        await until(lambda: any(a.name == "Docs" for a in agents_of(rid)))
        assert next(a for a in agents_of(rid) if a.name == "Long").status == "running"  # kept working meanwhile
        resolve_interaction(it.id, decision=None, answer="yes")
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded", run.summary
        assert "wait ended early" in seen["wait"] and "Also write the docs." in seen["note"]
        assert "long done" in run.summary and "docs done" in run.summary
        assert {a.name: a.status for a in agents_of(rid)} == {"Long": "succeeded", "Docs": "succeeded"}
    loop.run_until_complete(go())


def test_message_resumes_paused_planner(loop):
    async def go():
        SCRIPTS["planner"][:] = [
            slow(AIMessage("", tool_calls=[call("echo", text="a")]), 0.3),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary=" / ".join(
                str(m.content) for m in msgs if m.type == "human"))]),
        ]
        rid = new_run("resume on message")
        manager.start(rid)
        await asyncio.sleep(0.1)
        assert manager.pause(rid, "planner") == 1
        await asyncio.sleep(1.0)
        assert manager.is_active(rid) and manager.is_paused(rid, "planner")  # held at the gate
        assert manager.note(rid, "Switch to plan B.")
        assert not manager.is_paused(rid, "planner")
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded" and "Switch to plan B." in run.summary
        assert any(e.agent == "planner" and e.text == "Resumed by your message" for e in events_of(rid))
    loop.run_until_complete(go())


def test_planner_cannot_finish_while_agents_run(loop):
    async def go():
        seen: dict[str, str] = {}

        def after_refusal(msgs):
            seen["refusal"] = str(msgs[-1].content)
            return AIMessage("", tool_calls=[call("wait_for_agents")])

        SCRIPTS["Busy"] = [AIMessage("", tool_calls=[call("ask_human", question="Still there?")]),
                           AIMessage("", tool_calls=[call("finish", success=True, summary="busy done")])]
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("spawn_agent", name="Busy", instructions="x", task="y", toolsets=[])]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary="too early")]),
            after_refusal,
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary=str(msgs[-1].content))]),
        ]
        rid = new_run("finish guard")
        manager.start(rid)
        it = await pending(rid)
        await until(lambda: "refusal" in seen)
        resolve_interaction(it.id, decision=None, answer="yes")
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded" and "busy done" in run.summary
        assert "Not finished" in seen["refusal"] and "Busy" in seen["refusal"]
        assert agents_of(rid)[0].status == "succeeded"  # an early finish didn't cancel it
    loop.run_until_complete(go())
