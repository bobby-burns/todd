"""End-to-end flow tests with a scripted fake model (no API keys needed).

Covers: plugin loading, parallel tool calls, spend policy + human approval, ledger, delegation to the code
agent (against a real sandbox server if SANDBOX_URL is reachable), checkpoint resume, cancellation.

Run:  TODD_DATA_DIR=$(mktemp -d) TODD_PLUGINS_DIR=tests/plugins pytest -q tests
"""

from __future__ import annotations


import pytest
from langchain_core.messages import AIMessage

from todd import registry
from todd.db import LedgerEntry, select, session, get_run
from todd.orchestrator import manager
from todd.runtime import resolve_interaction

from .helpers import SCRIPTS, agents_of, call, events_of, new_run, pending, sandbox_up, wait_status


def test_plugin_loaded():
    toolsets = registry.all_toolsets()
    names = [t.name for t in registry.planner_tools(toolsets)]
    assert "echo" in names and "fake_purchase" in names
    assert "shout" in [t.name for t in toolsets["demo"].tools] and "shout" not in names
    assert {"spawn_agent", "wait_for_agents", "check_accounts"} <= set(names)
    assert "shell" not in names  # the planner orchestrates; hands-on tools go to spawned agents


def test_parallel_tools_spend_approval_and_finish(loop):
    async def go():
        SCRIPTS["planner"][:] = [
            AIMessage("Doing two things at once.", tool_calls=[call("echo", text="hello"),
                                                              call("fake_purchase", amount_usd=30.0, item="logo")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", summary="done: " + "|".join(
                m.content for m in msgs if m.type == "tool"), success=True)]),
        ]
        rid = new_run("buy a logo and echo")
        manager.start(rid)
        it = await pending(rid)
        assert it.kind == "spend" and it.data["amount_usd"] == 30.0
        assert get_run(rid).status == "waiting"
        resolve_interaction(it.id, decision="approve", answer="ok")
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded", run.summary
        assert "echo: hello" in run.summary and "purchased logo" in run.summary
        assert run.spent_usd == 30.0
        with session() as s:
            led = s.exec(select(LedgerEntry).where(LedgerEntry.run_id == rid)).all()
        assert len(led) == 1 and led[0].approved_by == "human" and led[0].status == "completed"
        kinds = [(e.agent, e.kind) for e in events_of(rid)]
        assert ("planner", "thought") in kinds and ("planner", "tool_call") in kinds
    loop.run_until_complete(go())


def test_small_spend_auto_approved_and_denial(loop):
    async def go():
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("fake_purchase", amount_usd=5.0, item="sticker")]),
            AIMessage("", tool_calls=[call("fake_purchase", amount_usd=100.0, item="car")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", summary=msgs[-1].content, success=False)]),
        ]
        rid = new_run("spend test", budget=50)
        manager.start(rid)
        it = await pending(rid)  # the $100 one; the $5 one was auto-approved
        assert it.data["amount_usd"] == 100.0 and "budget" in it.data["why_asking"]
        resolve_interaction(it.id, decision="deny", answer="too expensive")
        run = await wait_status(rid, {"succeeded", "failed"})
        assert "DENIED" in run.summary and run.spent_usd == 5.0
    loop.run_until_complete(go())


@pytest.mark.skipif(not sandbox_up(), reason="sandbox server not running")
def test_spawned_agent_uses_sandbox_and_plugin_toolset(loop):
    async def go():
        SCRIPTS["Site Builder"] = [
            AIMessage("Writing the file first.", tool_calls=[call("write_file", path="app/hello.txt", content="hi from todd")]),
            AIMessage("", tool_calls=[call("shell", cmd="cat app/hello.txt && ls"), call("shout", text="parallel")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", summary="built: " + msgs[-2].content, success=True)]),
        ]
        SCRIPTS["planner"][:] = [
            AIMessage("I'll spawn a builder.", tool_calls=[call(
                "spawn_agent", name="Site Builder", instructions="You build small sites.", task="write hello.txt",
                toolsets=["sandbox", "demo"])]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", summary=msgs[-1].content, success=True)]),
        ]
        rid = new_run("code test")
        manager.start(rid)
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded", run.summary
        assert "hi from todd" in run.summary
        (agent,) = agents_of(rid)
        assert agent.name == "Site Builder" and agent.status == "succeeded" and agent.toolsets == ["sandbox", "demo"]
        evs = events_of(rid)
        assert any(e.agent == agent.id and e.kind == "tool_call" and e.data["tool"] == "shell" for e in evs)
        assert any(e.agent == agent.id and e.kind == "thought" for e in evs)
        assert any(e.agent == "planner" and e.kind == "agent_spawned" for e in evs)
    loop.run_until_complete(go())


def test_background_agents_run_in_parallel(loop):
    async def go():
        started: dict[str, float] = {}

        def slow(name, reply):
            async def _noop():
                return None

            def step(msgs):
                import time
                started.setdefault(name, time.monotonic())
                return AIMessage("", tool_calls=[call("echo", text=reply)])
            return step

        SCRIPTS["Alpha"] = [slow("Alpha", "a1"),
                            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary="alpha saw: " + " | ".join(
                                str(m.content) for m in msgs if m.type == "human"))])]
        SCRIPTS["Beta"] = [slow("Beta", "b1"), AIMessage("", tool_calls=[call("finish", success=True, summary="beta done")])]
        SCRIPTS["planner"][:] = [
            AIMessage("Two agents in parallel.", tool_calls=[
                call("spawn_agent", name="Alpha", instructions="a", task="task A", toolsets=["demo"], background=True),
                call("spawn_agent", name="Beta", instructions="b", task="task B", toolsets=["demo"], background=True)]),
            lambda msgs: AIMessage("", tool_calls=[call("wait_for_agents")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary=msgs[-1].content)]),
        ]
        rid = new_run("parallel agents")
        manager.start(rid)
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded", run.summary
        assert "beta done" in run.summary and "alpha saw" in run.summary
        names = {a.name: a for a in agents_of(rid)}
        assert set(names) == {"Alpha", "Beta"} and all(a.status == "succeeded" for a in names.values())
        assert all(a.background for a in names.values())
    loop.run_until_complete(go())


def test_unknown_toolset_and_cancel_agent(loop):
    async def go():
        SCRIPTS["Stuck"] = [AIMessage("", tool_calls=[call("ask_human", question="Which color?")])]
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("spawn_agent", name="Bad", instructions="x", task="y", toolsets=["nope"])]),
            lambda msgs: AIMessage("", tool_calls=[call("spawn_agent", name="Stuck", instructions="x", task="y",
                                                        toolsets=[], background=True)]),
            lambda msgs: AIMessage("", tool_calls=[call("wait_for_agents")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary=" || ".join(
                str(m.content) for m in msgs if m.type == "tool"))]),
        ]
        rid = new_run("cancel agent")
        manager.start(rid)
        it = await pending(rid)
        (stuck,) = [a for a in agents_of(rid) if a.name == "Stuck"]
        assert it.agent == stuck.id  # the question belongs to the spawned agent's window
        assert manager.cancel_agent(rid, stuck.id)
        run = await wait_status(rid, {"succeeded", "failed"})
        assert "unknown toolset" in run.summary and "cancelled" in run.summary
        assert [a for a in agents_of(rid) if a.name == "Stuck"][0].status == "cancelled"
    loop.run_until_complete(go())


def test_resume_from_checkpoint(loop):
    async def go():
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("echo", text="step-one")]),
            RuntimeError("model provider went down"),
        ]
        rid = new_run("resume test")
        manager.start(rid)
        run = await wait_status(rid, {"failed"})
        assert "went down" in (run.summary or "")
        # resume: the planner should see step-one's result already in history (no re-run of echo)
        SCRIPTS["planner"][:] = [
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary="history: " + " ".join(
                m.content for m in msgs if m.type == "tool"))]),
        ]
        manager.start(rid, resume=True)
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded" and "echo: step-one" in run.summary
        echo_calls = [e for e in events_of(rid) if e.kind == "tool_call" and e.data.get("tool") == "echo"]
        assert len(echo_calls) == 1
    loop.run_until_complete(go())


def test_cancel_while_waiting(loop):
    async def go():
        SCRIPTS["planner"][:] = [AIMessage("", tool_calls=[call("ask_human", question="Which color?")])]
        rid = new_run("cancel test")
        manager.start(rid)
        await pending(rid)
        assert manager.cancel(rid)
        run = await wait_status(rid, {"cancelled"})
        assert run.status == "cancelled"
    loop.run_until_complete(go())


def test_ask_human_answer_and_user_note(loop):
    async def go():
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("ask_human", question="Which color?")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary=" / ".join(
                str(m.content) for m in msgs if m.type in ("tool", "human")))]),
        ]
        rid = new_run("question test")
        manager.start(rid)
        it = await pending(rid)
        manager.note(rid, "also make it fast")
        resolve_interaction(it.id, decision=None, answer="blue")
        run = await wait_status(rid, {"succeeded", "failed"})
        assert "blue" in run.summary and "also make it fast" in run.summary
    loop.run_until_complete(go())
