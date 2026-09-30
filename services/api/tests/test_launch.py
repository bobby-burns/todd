"""The launch plan (asked at the start, without stopping the run) and what it controls: going live, public
repositories; plus the launch checklist read from a run's files, and questions with options."""

from __future__ import annotations

import asyncio
import json

import pytest

from todd import gates, launch, readiness, workspace
from todd.db import Interaction, get_run, select, session
from todd.runtime import RunContext, resolve_interaction
from todd.sdk import ToolError, set_ctx, set_current_agent

from .helpers import new_run, pending

PROMPT = ("Make a website that gives me multiple choice/other style (no typing) Ai and ML questions everyday but also "
          "lets me answer as many as I want. Add a streaks and other features")


def test_which_goals_get_a_launch_plan():
    assert launch.shippable(PROMPT)
    assert launch.shippable("build a landing page for my bakery")
    assert not launch.shippable("explain how transformers work")
    assert not launch.shippable("summarize my site's analytics report")  # nothing to make


def test_parse_the_answer():
    p = launch.parse(json.dumps({"domain": "own", "domain_name": "https://QuizDaily.com/", "repo": "public",
                                 "live": "auto"}))
    assert p == {"domain": "own", "domain_name": "quizdaily.com", "repo": "public", "live": "auto", "note": ""}
    assert launch.parse("put it on my domain please")["note"] == "put it on my domain please"
    assert launch.parse(json.dumps({"domain": "moon", "live": "yes"})) == {**launch.DEFAULT}
    assert "private GitHub repository" in launch.describe(launch.DEFAULT)


def _cards(run_id):
    with session() as s:
        return [i for i in s.exec(select(Interaction).where(Interaction.run_id == run_id)).all()]


def test_launch_card_is_asked_up_front_without_stopping_the_run(loop):
    from todd.orchestrator import RunManager

    async def go():
        rid = new_run(PROMPT)
        ctx = RunContext(rid)
        note = await RunManager._launch_note(ctx, PROMPT, continuing=False)
        assert "Don't wait for it" in note
        it = await pending(rid, background=True)
        assert it.data["kind_hint"] == "launch" and it.data["background"] is True
        assert get_run(rid).status != "waiting"  # agents keep building; nobody is held up
        assert await launch.ask(ctx) == "asked" and len(_cards(rid)) == 1  # once per run
        resolve_interaction(it.id, decision=None, answer=json.dumps(
            {"domain": "own", "domain_name": "quizdaily.com", "repo": "private", "live": "ask"}))
        plan = await launch.wait(ctx, timeout=5)
        assert plan and plan["domain_name"] == "quizdaily.com"
        msg = ctx.user_notes.get_nowait()
        assert msg.startswith("[launch plan from the human]") and "quizdaily.com" in msg
        # a resumed run reads the answer back and reminds the planner
        again = RunContext(rid)
        assert launch.get(again)["domain_name"] == "quizdaily.com"
        assert "quizdaily.com" in await RunManager._launch_note(again, PROMPT, continuing=True)
        # nothing to launch: no card
        other = RunContext(new_run("explain transformers"))
        assert await RunManager._launch_note(other, "explain transformers", continuing=False) == ""

    loop.run_until_complete(go())


def test_unanswered_card_closes_with_the_run(loop):
    async def go():
        rid = new_run(PROMPT)
        ctx = RunContext(rid)
        await launch.ask(ctx, "the quiz site")
        launch.close(ctx)
        await asyncio.sleep(0)
        assert [c.status for c in _cards(rid)] == ["cancelled"] and launch.get(ctx) is None

    loop.run_until_complete(go())


def test_going_live_follows_the_plan(loop, monkeypatch):
    from todd.tools import sandbox
    from todd.tools.sandbox_tools import gh

    ran: list[str] = []

    async def exec_(cmd, cwd, timeout=300, env=None, signed_in=False):
        ran.append(cmd)
        return {"exit_code": 0, "output": "ok"}

    monkeypatch.setattr(sandbox, "exec_", exec_)

    async def go():
        from todd import vault

        vault.set_secret("GITHUB_TOKEN", "ghp_" + "l" * 36)
        rid = new_run(PROMPT)
        ctx = RunContext(rid)
        set_ctx(ctx)
        set_current_agent("a_host")

        # no plan yet: going live asks; "no" keeps it on localhost
        t = asyncio.create_task(gates.approve_live(ctx, "a_host", "a production deploy on Vercel"))
        it = await pending(rid)
        assert it.data["kind_hint"] == "live" and it.prompt.startswith("Put it live?")
        resolve_interaction(it.id, decision="deny", answer=None)
        with pytest.raises(ToolError, match="localhost"):
            await t
        # "yes" counts for the rest of the run, for every agent
        t = asyncio.create_task(gates.approve_live(ctx, "a_host", "a production deploy on Vercel"))
        resolve_interaction((await pending(rid)).id, decision="approve", answer=None)
        await t
        await gates.approve_live(ctx, "a_other", "pointing a domain at the site")  # no second question
        assert not [i for i in _cards(rid) if i.status == "pending"]

        # "when it's ready": no questions at all
        ctx2 = RunContext(new_run(PROMPT))
        ctx2.launch = launch.parse(json.dumps({"live": "auto"}))  # type: ignore[attr-defined]
        await gates.approve_live(ctx2, "a_host", "a production deploy on Vercel")

        # repositories: private by default; public only when the plan says so (or the human approves)
        await gh.ainvoke({"command": "repo create quiz --source . --push"})
        assert "--private" in ran[-1]
        t = asyncio.create_task(gh.ainvoke({"command": "repo create quiz2 --public --source ."}))
        it = await pending(rid)
        assert "public GitHub repository" in it.prompt
        resolve_interaction(it.id, decision="deny", answer=None)
        with pytest.raises(ToolError):
            await t
        ctx.launch = launch.parse(json.dumps({"repo": "public"}))  # type: ignore[attr-defined]
        await gh.ainvoke({"command": "repo create quiz3 --public --source ."})
        assert "--public" in ran[-1]
        vault.delete_secret("GITHUB_TOKEN")

    loop.run_until_complete(go())


def test_live_commands_and_requests():
    cc = gates.check_command
    assert cc("vercel", ["deploy", "--prod", "--yes"])[0] == "live"
    assert cc("vercel", ["deploy", "--yes"])[0] == "live"  # a new project's first deploy is production
    assert cc("vercel", ["deploy", "--target=preview", "--yes"])[0] == "deploy"  # previews ask too
    assert cc("vercel", ["domains", "add", "quizdaily.com"])[0] == "live"
    assert cc("netlify", ["deploy", "--dir", "dist"])[0] == "deploy" and cc("netlify", ["deploy", "--prod"])[0] == "live"
    assert cc("firebase", ["deploy"])[0] == "live" and cc("firebase", ["hosting:channel:deploy", "pr"])[0] == "deploy"
    assert cc("vercel", ["env", "ls"]) is None and cc("netlify", ["status"]) is None  # not deploys
    assert cc("cloudflare", ["pages", "deploy", "dist", "--branch", "preview"])[0] == "deploy"
    assert cc("railway", ["up"])[0] == "live"
    assert gates.check_request("POST", "https://api.vercel.com/v13/deployments", '{"target": "production"}')[0] == "live"
    assert gates.check_request("POST", "https://api.vercel.com/v13/deployments", '{"name": "quiz"}')[0] == "deploy"
    assert gates.check_request("POST", "https://api.vercel.com/v10/projects/quiz/domains", "{}")[0] == "live"


def test_vercel_tools_ask_before_going_live(loop, monkeypatch):
    from todd.tools import infra

    calls: list[str] = []

    async def fake_vercel(method, path, *, json=None, params_=None):
        calls.append(f"{method} {path}")
        return {"id": "p1", "name": "quiz"}

    monkeypatch.setattr(infra, "_vercel", fake_vercel)

    async def go():
        ctx = RunContext(new_run(PROMPT))
        set_ctx(ctx)
        set_current_agent("a_host")
        await infra.vercel_create_project.ainvoke({"name": "quiz"})  # no Git connection: nothing goes live
        assert calls == ["POST /v11/projects"]
        t = asyncio.create_task(infra.vercel_create_project.ainvoke({"name": "quiz", "github_repo": "me/quiz"}))
        it = await pending(ctx.run_id)
        assert "every push to main goes live" in it.prompt and len(calls) == 1
        resolve_interaction(it.id, decision="approve", answer=None)
        await t
        assert len(calls) == 2
        await infra.vercel_add_domain.ainvoke({"project": "quiz", "domain": "quizdaily.com", "redirect_www": False})
        assert calls[-1] == "POST /v10/projects/quiz/domains"  # approved once for the run

    loop.run_until_complete(go())


def test_questions_can_offer_choices(loop):
    from todd.tools.human import ask_human

    async def go():
        ctx = RunContext(new_run("pick a domain"))
        set_ctx(ctx)
        t = asyncio.create_task(ask_human.ainvoke({"question": "Which domain?",
                                                   "options": ["quizdaily.com ($12/yr)", "dailyml.dev ($15/yr)"]}))
        it = await pending(ctx.run_id)
        assert it.data["options"] == ["quizdaily.com ($12/yr)", "dailyml.dev ($15/yr)"]
        resolve_interaction(it.id, decision=None, answer="dailyml.dev ($15/yr)")
        assert await t == "dailyml.dev ($15/yr)"

    loop.run_until_complete(go())


def test_launch_checklist_reads_the_project(loop, monkeypatch):
    files = {
        "quiz/package.json": json.dumps({"dependencies": {"next": "16.0.0", "react": "19.0.0"}}),
        "quiz/app/layout.tsx": "export const metadata = { title: 'Quiz', description: 'Daily AI questions',"
                               " openGraph: { images: ['/og.png'] } }",
        "quiz/app/favicon.ico": "",
        "quiz/app/robots.ts": "export default function robots() {}",
        "quiz/app/not-found.tsx": "export default function NotFound() {}",
        "quiz/next.config.ts": "export default {}",
        "quiz/node_modules/x/robots.txt": "",
    }

    async def tree(run_id):
        return {"exists": True, "entries": [{"path": p, "dir": False} for p in files], "truncated": False}

    async def view(run_id, path, reveal=False):
        return {"kind": "text", "content": files[path]}

    monkeypatch.setattr(workspace, "tree", tree)
    monkeypatch.setattr(workspace, "view", view)
    r = loop.run_until_complete(readiness.check("r1"))
    got = {i["id"]: i["ok"] for i in r["items"]}
    assert r["web"] and r["framework"] == "Next.js" and r["project"] == "quiz"
    assert got["meta"] and got["social"] and got["icons"] and got["robots"] and got["404"]
    assert not got["sitemap"] and not got["llms"] and not got["headers"]
    assert "llms.txt" in r["prompt"] and "sitemap.xml" in r["prompt"] and "security headers" in r["prompt"]
    assert r["missing"] == 3  # the optional ones (manifest, privacy) don't count

    files.clear()
    files["notes.md"] = "# notes"
    assert loop.run_until_complete(readiness.check("r2")) == {"web": False, "items": []}


def test_deploying_comes_last_and_previews_ask_too(loop):
    from types import SimpleNamespace

    async def go():
        rid = new_run(PROMPT)
        ctx = RunContext(rid)
        set_ctx(ctx)
        set_current_agent("a_ship")
        building = asyncio.get_running_loop().create_future()  # a builder that's still working
        ctx.agents["a_build"] = SimpleNamespace(id="a_build", name="Site Builder", toolsets=["sandbox"], task=building)
        ctx.agents["a_research"] = SimpleNamespace(id="a_research", name="Research", toolsets=["web"],
                                                   task=asyncio.get_running_loop().create_future())
        with pytest.raises(ToolError, match="Site Builder is still working"):
            await gates.approve_live(ctx, "a_ship", "a preview deploy on Vercel", kind="deploy")
        assert not [i for i in _cards(rid) if i.status == "pending"]  # refused before asking anyone
        building.set_result(None)  # the build is done (a research helper without the sandbox doesn't block)

        # a preview asks, and approving it covers previews only
        t = asyncio.create_task(gates.approve_live(ctx, "a_ship", "a preview deploy on Vercel", kind="deploy"))
        it = await pending(rid)
        assert it.prompt.startswith("Deploy a preview?")
        resolve_interaction(it.id, decision="approve", answer=None)
        await t
        await gates.approve_live(ctx, "a_ship", "another preview", kind="deploy")  # no second question
        t = asyncio.create_task(gates.approve_live(ctx, "a_ship", "a production deploy on Vercel"))
        it = await pending(rid)
        assert it.prompt.startswith("Put it live?")
        resolve_interaction(it.id, decision="approve", answer=None)
        await t

    loop.run_until_complete(go())


def test_two_agents_never_change_the_same_file(loop, monkeypatch):
    from types import SimpleNamespace

    from todd.tools import sandbox
    from todd.tools.sandbox_tools import write_file

    written: list[str] = []

    async def write(path, content):
        written.append(path)
        return {"ok": True}

    monkeypatch.setattr(sandbox, "write", write)

    async def go():
        ctx = RunContext(new_run(PROMPT))
        set_ctx(ctx)
        loop_ = asyncio.get_running_loop()
        builder = SimpleNamespace(id="a_build", name="Site Builder", toolsets=["sandbox"], task=loop_.create_future())
        bank = SimpleNamespace(id="a_bank", name="Question Bank", toolsets=["sandbox"], task=loop_.create_future())
        ctx.agents.update({"a_build": builder, "a_bank": bank})

        set_current_agent("a_build")
        await write_file.ainvoke({"path": "app/page.tsx", "content": "export default function Home() {}"})
        set_current_agent("a_bank")  # the helper writes its own file, which the builder will read
        await write_file.ainvoke({"path": "data/questions.json", "content": "[]"})
        with pytest.raises(ToolError, match="Site Builder is working on app/page.tsx"):
            await write_file.ainvoke({"path": "app/page.tsx", "content": "// mine now"})
        set_current_agent("a_build")
        with pytest.raises(ToolError, match="Question Bank is working on data/questions.json"):
            await write_file.ainvoke({"path": "./data/questions.json", "content": "[{}]"})
        await write_file.ainvoke({"path": "app/page.tsx", "content": "// still the builder's"})
        bank.task.set_result(None)  # once the helper is done, its file is free
        await write_file.ainvoke({"path": "data/questions.json", "content": "[{}]"})
        assert len(written) == 4

    loop.run_until_complete(go())
