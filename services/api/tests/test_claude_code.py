"""End to end: the Claude Code engine, with the real `claude` CLI talking to a scripted Messages API.

Covers: planner → spawned agent over Todd's MCP bridge, thinking/thoughts/tool events, finish summaries, nudges,
pause + human messages delivered mid-run, spend approvals held open across a long tool call, cancel, resume of the
planner's session after an interruption, the not-signed-in error, and (with a browser) direct browser control.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import threading
import time

import pytest
import uvicorn

from todd import settings
from todd.config import config
from todd.orchestrator import manager
from todd.runtime import resolve_interaction

from . import fake_anthropic as fake
from .helpers import agents_of, events_of, new_run, pending, wait_status

pytestmark = pytest.mark.skipif(not shutil.which(config.claude_bin), reason="claude CLI not installed")

T = "mcp__todd__"


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module", autouse=True)
def cc(loop):
    fport = _port()
    threading.Thread(target=uvicorn.run, kwargs=dict(app=fake.app, host="127.0.0.1", port=fport, log_level="warning"),
                     daemon=True).start()
    from todd.main import app

    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = loop.create_task(server.serve())
    loop.run_until_complete(asyncio.sleep(1.0))
    old_url = config.internal_url
    config.internal_url = f"http://127.0.0.1:{port}"
    os.environ["TODD_TEST_ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{fport}"
    os.environ["TODD_TEST_ANTHROPIC_API_KEY"] = "sk-test"
    settings.update({"engine": "claude_code"})
    yield
    settings.update({"engine": "api"})
    config.internal_url = old_url
    os.environ.pop("TODD_TEST_ANTHROPIC_BASE_URL", None)
    os.environ.pop("TODD_TEST_ANTHROPIC_API_KEY", None)
    server.should_exit = True
    loop.run_until_complete(task)


@pytest.fixture(autouse=True)
def clean_scripts():
    fake.SCRIPTS.clear()
    fake.LOG.clear()
    yield


def step(*blocks, delay: float = 0) -> dict:
    return {"blocks": list(blocks), "delay": delay}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def think(t: str) -> dict:
    return {"type": "thinking", "thinking": t}


def tool(_name: str, **inp) -> dict:
    return {"type": "tool_use", "name": T + _name, "input": inp}


def finish(summary: str, success: bool = True) -> dict:
    return tool("finish", summary=summary, success=success)


PLANNER = "# Role: planner"


def worker(name: str) -> str:
    return f"# You are: {name}"


def test_planner_spawns_agent_end_to_end(loop):
    fake.SCRIPTS[PLANNER] = [
        step(think("One agent is enough."), text("Plan: one writer."),
             tool("spawn_agent", name="Writer", instructions="Echo things.", task="Echo hi, then finish.",
                  toolsets=["demo"])),
        step(finish("Done: all\nAgents: Writer — echoed")),
    ]
    fake.SCRIPTS[worker("Writer")] = [
        step(text("Echoing."), tool("echo", text="hi")),
        step(finish("Done: echoed\nOutputs: echo: hi\nHow: demo toolset\nLeft / needs you: nothing")),
    ]

    async def go():
        rid = new_run("write something")
        manager.start(rid)
        run = await wait_status(rid, {"succeeded", "failed"}, timeout=90)
        evs = events_of(rid)
        assert run.status == "succeeded", [e.text for e in evs if e.kind == "error"]
        (w,) = agents_of(rid)
        assert w.name == "Writer" and w.status == "succeeded" and w.model == "claude-opus-5-5"
        kinds = {(e.agent, e.kind) for e in evs}
        assert ("planner", "thinking") in kinds and ("planner", "thought") in kinds
        call = next(e for e in evs if e.kind == "tool_call" and e.data.get("tool") == "echo")
        res = next(e for e in evs if e.kind == "tool_result" and e.data.get("call_id") == call.data["call_id"])
        assert call.agent == w.id and "echo: hi" in res.text
        summaries = {e.agent: e.text for e in evs if e.kind == "summary"}
        assert summaries[w.id].startswith("Done: echoed") and summaries["planner"].startswith("Done: all")
        assert any((e.data or {}).get("engine") == "claude_code" for e in evs if e.agent == "system")
        # the agent only ever saw Todd's tools (no Bash/Read/…)
        tools_seen = {t for entry in fake.LOG if entry.get("key") == worker("Writer") for t in entry["tools"]}
        assert tools_seen and all(t.startswith(T) for t in tools_seen)
        assert T + "echo" in tools_seen and T + "finish" in tools_seen and T + "shout" in tools_seen

    loop.run_until_complete(go())


def test_nudge_pause_and_message(loop):
    fake.SCRIPTS[PLANNER] = [
        step(tool("spawn_agent", name="Slowpoke", instructions="x", task="Do three echoes.", toolsets=["demo"],
                  background=True)),
        step(tool("wait_for_agents")),
        step(finish("Done: ok")),
    ]
    fake.SCRIPTS[worker("Slowpoke")] = [
        step(text("Thinking about it, no tools yet.")),  # ends the turn without finish -> nudge
        step(text("Step one."), tool("echo", text="one"), delay=0.3),
        step(text("Step two."), tool("echo", text="two"), delay=0.3),
        step(finish("Done: echoes"), delay=0.3),
    ]

    async def go():
        rid = new_run("nudge test")
        manager.start(rid)
        for _ in range(400):
            ags = agents_of(rid)
            if ags and any(e.kind == "tool_call" and e.agent == ags[0].id for e in events_of(rid)):
                break
            await asyncio.sleep(0.05)
        aid = agents_of(rid)[0].id
        assert manager.pause(rid, aid) == 1
        assert manager.note(rid, "Use uppercase from now on.", agent_id=aid)
        await asyncio.sleep(2.5)
        n = len([e for e in events_of(rid) if e.agent == aid and e.kind == "tool_call"])
        await asyncio.sleep(2.0)
        assert len([e for e in events_of(rid) if e.agent == aid and e.kind == "tool_call"]) == n  # held at the gate
        manager.pause(rid, aid, resume=True)
        run = await wait_status(rid, {"succeeded", "failed"}, timeout=90)
        assert run.status == "succeeded"
        msgs = [e for e in fake.LOG if e.get("key") == worker("Slowpoke")]
        blob = " ".join(str(e.get("last")) for e in msgs)
        assert "Continue working using your tools" in blob  # the nudge
        assert "Use uppercase from now on." in blob  # the human's message reached the agent
        assert agents_of(rid)[0].status == "succeeded"

    loop.run_until_complete(go())


def test_spend_approval_through_mcp(loop):
    fake.SCRIPTS[PLANNER] = [
        step(text("Buying."), tool("fake_purchase", amount_usd=40, item="domain")),
        step(finish("Done: bought")),
    ]

    async def go():
        settings.update({"spend_policy": {"auto_approve_under_usd": 15, "always_ask": False}})
        rid = new_run("buy it", budget=100)
        manager.start(rid)
        it = await pending(rid, timeout=60)
        assert it.kind == "spend" and it.data["amount_usd"] == 40
        await asyncio.sleep(3)  # the MCP tool call stays open while the human decides
        resolve_interaction(it.id, decision="approve", answer=None)
        run = await wait_status(rid, {"succeeded", "failed"}, timeout=60)
        assert run.status == "succeeded" and run.spent_usd == 40
        res = next(e for e in events_of(rid) if e.kind == "tool_result" and e.data.get("tool") == "fake_purchase")
        assert "purchased domain" in res.text

    loop.run_until_complete(go())


def test_cancel_agent_kills_session(loop):
    fake.SCRIPTS[PLANNER] = [
        step(tool("spawn_agent", name="Asker", instructions="x", task="Ask the human.", toolsets=[], background=True)),
        step(tool("wait_for_agents")),
        step(finish("Done: asker was stopped")),
    ]
    fake.SCRIPTS[worker("Asker")] = [step(tool("ask_human", question="What colour?"))]

    async def go():
        rid = new_run("cancel test")
        manager.start(rid)
        it = await pending(rid, timeout=60)
        aid = it.agent
        assert manager.cancel_agent(rid, aid)
        run = await wait_status(rid, {"succeeded", "failed"}, timeout=60)
        assert run.status == "succeeded"
        a = agents_of(rid)[0]
        assert a.status == "cancelled"
        s = next(e.text for e in events_of(rid) if e.kind == "summary" and e.agent == aid)
        assert "Stopped before finishing" in s

    loop.run_until_complete(go())


def test_resume_continues_planner_session(loop):
    fake.SCRIPTS[PLANNER] = [
        step(text("Need input."), tool("ask_human", question="Which domain?")),
        # after the interruption the transcript may hold extra turns; any later turn finishes
        *[step(finish("Done: resumed and finished")) for _ in range(4)],
    ]

    async def go():
        rid = new_run("resume test")
        manager.start(rid)
        await pending(rid, timeout=60)
        manager.cancel(rid)
        await wait_status(rid, {"cancelled"}, timeout=30)
        manager.start(rid, resume=True)
        run = await wait_status(rid, {"succeeded", "failed"}, timeout=60)
        assert run.status == "succeeded" and run.summary.startswith("Done: resumed"), run.summary
        last = [e for e in fake.LOG if e.get("key") == PLANNER][-1]
        assert last["n_messages"] >= 3  # the resumed session carried its history
        assert "Continue from where you left off" in str([e["last"] for e in fake.LOG if e.get("key") == PLANNER])

    loop.run_until_complete(go())


def test_not_signed_in_is_explained(loop):
    fake.SCRIPTS[PLANNER] = [{"error": "auth"}]  # the API rejects the credentials (e.g. signed out / expired)

    async def go():
        rid = new_run("auth test")
        t0 = time.time()
        manager.start(rid)
        run = await wait_status(rid, {"succeeded", "failed"}, timeout=60)
        assert time.time() - t0 < 30  # fails fast instead of sitting through the CLI's retries
        assert run.status == "failed"
        err = " ".join(e.text for e in events_of(rid) if e.kind == "error")
        assert "claude auth login" in err

    loop.run_until_complete(go())


def _browser_up() -> bool:
    try:
        import httpx

        return httpx.get(f"http://{config.browser_cdp_host}:{config.browser_cdp_port}/json/version", timeout=1).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _browser_up(), reason="no browser reachable over CDP")
def test_direct_browser_control(loop):
    page = os.getenv("TODD_TEST_PAGE", "http://127.0.0.1:8765/asc.html")
    fake.SCRIPTS[PLANNER] = [
        step(tool("spawn_agent", name="Surfer", instructions="x", task="Read the subtitle.", toolsets=["browser"])),
        step(finish("Done: read it")),
    ]
    fake.SCRIPTS[worker("Surfer")] = [
        step(tool("browser_navigate", url=page)),  # before browser_start -> a helpful error
        step(tool("browser_start", why_not_api="No App Store Connect API key in the vault.", start_url=page)),
        step(tool("browser_scroll", down=True, pages=0.5)),
        step(tool("browser_done", result="Subtitle: Scan receipts, waste less food")),
        step(finish("Done: read the subtitle\nOutputs: Scan receipts, waste less food")),
    ]

    async def go():
        rid = new_run("browser test")
        manager.start(rid)
        run = await wait_status(rid, {"succeeded", "failed"}, timeout=120)
        evs = events_of(rid)
        assert run.status == "succeeded", [e.text for e in evs if e.kind == "error"]
        aid = agents_of(rid)[0].id
        first = next(e for e in evs if e.kind == "tool_result" and e.data.get("tool") == "browser_navigate")
        assert "browser_start" in first.text
        steps = [e for e in evs if e.kind == "browser_step" and e.agent == aid]
        assert len(steps) >= 2 and all(s.data.get("screenshot") for s in steps)
        start = next(e for e in evs if e.kind == "tool_result" and e.data.get("tool") == "browser_start")
        assert "Scan receipts" in start.text and "[" in start.text  # indexed elements
        assert any(e.data.get("browser_reason") for e in evs if e.kind == "status")
        assert any(e.data.get("browser") == "end" for e in evs if e.kind == "status")
        ctx = manager.contexts[rid]
        assert not ctx.browser_lock.locked()
        # the model received the screenshot as an image in the tool result
        blob = str([e["last"] for e in fake.LOG if e.get("key") == worker("Surfer")])
        assert "'type': 'image'" in blob

    loop.run_until_complete(go())


@pytest.mark.skipif(not _browser_up(), reason="no browser reachable over CDP")
def test_cancelled_agent_waiting_for_browser_leaves_no_lock(loop):
    page = os.getenv("TODD_TEST_PAGE", "http://127.0.0.1:8765/asc.html")
    fake.SCRIPTS[PLANNER] = [
        step(tool("spawn_agent", name="Holder", instructions="x", task="Hold the browser.", toolsets=["browser"],
                  background=True),
             tool("spawn_agent", name="Waiter", instructions="x", task="Wait for the browser.", toolsets=["browser"],
                  background=True)),
        step(tool("wait_for_agents")),
        step(finish("Done: ok")),
    ]
    fake.SCRIPTS[worker("Holder")] = [
        step(tool("browser_start", why_not_api="Only reachable in the signed-in console.", start_url=page)),
        step(tool("ask_human", question="Hold on while I look around?")),
        step(tool("browser_done", result="looked")),
        step(finish("Done: looked")),
    ]
    fake.SCRIPTS[worker("Waiter")] = [
        step(tool("browser_start", why_not_api="Only reachable in the signed-in console.", start_url=page), delay=1.5),
        step(finish("Done: never")),
    ]

    async def go():
        rid = new_run("lock test")
        manager.start(rid)
        it = await pending(rid, timeout=90)  # Holder has the browser and is asking; Waiter is queued on the lock
        waiter = next(a for a in agents_of(rid) if a.name == "Waiter")
        for _ in range(100):
            if any(e.agent == waiter.id and "Waiting for the browser" in e.text for e in events_of(rid)):
                break
            await asyncio.sleep(0.1)
        assert manager.cancel_agent(rid, waiter.id)
        await asyncio.sleep(2)
        resolve_interaction(it.id, decision=None, answer="sure")
        run = await wait_status(rid, {"succeeded", "failed"}, timeout=90)
        assert run.status == "succeeded"
        ctx = manager.contexts[rid]
        assert not ctx.browser_lock.locked() and not getattr(ctx, "_browser_handles", {})
        assert next(a for a in agents_of(rid) if a.name == "Waiter").status == "cancelled"

    loop.run_until_complete(go())
