"""Sign in once, up-front: reading the accounts a goal needs from its prompt, the Sign in / Later card that pauses the
run until they're signed in (it closes itself), and what the planner is told afterwards."""

from __future__ import annotations

import asyncio

import pytest

from todd import accounts, needs, signin, vault
from todd.db import Interaction, select, session
from todd.runtime import resolve_interaction

from .test_connect import _ctx

ASC = ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY")


@pytest.fixture
def clean_vault(monkeypatch):
    names = ("GITHUB_TOKEN", "GH_TOKEN", "EXPO_TOKEN", "VERCEL_TOKEN", "STRIPE_SECRET_KEY", *ASC)
    for n in names:
        monkeypatch.delenv(n, raising=False)
        vault.delete_secret(n)
    from todd import connect

    for sid in connect.CONNECTORS:
        vault.delete_secret(connect.state_name(sid))
    yield


def ids(prompt: str) -> list[str]:
    return [s["id"] for s in needs.detect(prompt)]


def test_detect_reads_the_accounts_a_goal_needs(clean_vault):
    assert ids("Build a simple Expo iPhone app that tracks water intake and put it on TestFlight") == \
        ["expo", "app_store_connect", "apple_dev", "github"]
    assert ids("Launch a waitlist site for dropin.hockey with a launch post") == ["github", "vercel", "x"]
    got = ids("Deploy my landing page to Netlify and post it on Product Hunt and Twitter")
    assert "netlify" in got and "vercel" not in got and {"producthunt", "x"} <= set(got)  # they picked the host
    assert ids("Research rink apps and put them in a Notion table") == ["notion"]
    assert ids("fix the bug, 10x faster, the notion of speed") == []  # everyday words aren't services
    assert "play_console" in ids("Make an Android app") and "stripe" in ids("Build a web app that takes payments")
    why = {s["id"]: s["why"] for s in needs.detect("Build an iPhone app, on GitHub")}
    assert why["github"] == "named in your goal" and "iPhone" in why["expo"]
    assert ids("Deploy it to Render") == ["render"] and ids("Buy a domain for my site") == ["vercel"]
    # a video about an app needs no hosting or app-store accounts, and no TikTok account unless it's posted
    assert ids("Make a TikTok for my iPhone app and its website") == []
    assert ids("Make 3 reels for my web app https://quiz.example.com") == []
    assert ids("Make a short video for my web app and post it on TikTok") == ["tiktok"]
    assert {"github", "vercel"} <= set(ids("Build a website and a TikTok for it"))  # building still needs them
    assert ids("Reply to the comments on my TikTok") == ["tiktok"]  # the account itself is the work
    # talking about something isn't making it, and everyday words aren't services
    for prompt in ("Write a report on iOS 18 adoption", "Search google.com for cheap flights", "plot y based on x",
                   "Render a chart of my sales", "Summarize my subscriptions", "Fix the checkout bug",
                   "get the domain model right", "research SaaS pricing"):
        assert ids(prompt) == [], prompt


def test_the_card_says_why_it_needs_each_account(clean_vault):
    got = needs.detect("Build an iPhone app, on GitHub")
    use = {s["id"]: s["use"] for s in got}
    assert use["expo"] == "Builds the app in the cloud" and "TestFlight" in use["app_store_connect"]
    why = needs.explain(got)
    assert why.startswith("You're making an iPhone app") and why.endswith("You mentioned GitHub in your goal.")
    assert why.count("iPhone") == 1  # one sentence per reason, not per account
    assert needs.explain(needs.detect("Deploy it to Render and Railway")) == \
        "You mentioned Railway and Render in your goal."  # catalog order
    assert needs.explain([]) == ""


def test_detect_skips_what_todd_can_already_use(clean_vault):
    vault.set_secret("GITHUB_TOKEN", "gho_already")
    for n in ASC:
        vault.set_secret(n, "x")
    try:
        assert ids("Build an iPhone app") == ["expo"]  # GitHub and Apple work through their keys already
    finally:
        for n in ("GITHUB_TOKEN", *ASC):
            vault.delete_secret(n)


@pytest.fixture
def browser(monkeypatch):
    """A fake browser: `signed` is the set of services signed in right now."""
    state = {"signed": set(), "online": True, "probed": []}

    async def statuses(ids=None):
        cat = accounts.catalog()
        if not state["online"]:
            return {"browser_online": False, "accounts": []}
        return {"browser_online": True, "accounts": [
            {"id": i, "name": cat[i].name, "status": "signed_in" if i in state["signed"] else "signed_out",
             "source": "session cookie"} for i in ids or []]}

    async def verify(i):
        state["probed"].append(i)
        return {"id": i, "signed_in": i in state["signed"]}

    monkeypatch.setattr(accounts, "statuses", statuses)
    monkeypatch.setattr(accounts, "verify", verify)
    monkeypatch.setattr(signin, "POLL", 0.05)
    return state


async def _card(run_id: str) -> Interaction:
    for _ in range(200):
        with session() as s:
            it = s.exec(select(Interaction).where(Interaction.run_id == run_id)).first()
        if it:
            return it
        await asyncio.sleep(0.02)
    raise AssertionError("no sign-in card")


WANT = [{"id": "github", "name": "GitHub", "why": "code"}, {"id": "vercel", "name": "Vercel", "why": "hosting"}]


def test_gate_waits_and_closes_itself_once_signed_in(loop, browser):
    browser["signed"] = {"github"}

    async def go():
        ctx = _ctx([])
        task = asyncio.create_task(signin.gate(ctx, WANT))
        it = await _card(ctx.run_id)
        assert it.kind == "approval" and it.data["kind_hint"] == "signin" and it.status == "pending"
        assert [s["id"] for s in it.data["services"]] == ["vercel"]  # only what's missing is on the card
        assert "Vercel" in it.prompt
        await asyncio.sleep(0.2)
        assert not task.done()  # the run waits
        browser["signed"].add("vercel")  # the human signs in through the card
        res = await asyncio.wait_for(task, 5)
        assert res["signed_in"] == ["github", "vercel"] and res["later"] == [] and res["asked"]
        with session() as s:
            assert s.get(Interaction, it.id).status == "approved"  # closed for them
    loop.run_until_complete(go())


def test_gate_later_and_nothing_to_ask(loop, browser):
    async def go():
        ctx = _ctx([])
        task = asyncio.create_task(signin.gate(ctx, WANT))
        it = await _card(ctx.run_id)
        resolve_interaction(it.id, decision="deny", answer="Later")
        res = await asyncio.wait_for(task, 5)
        assert res["later"] == ["github", "vercel"] and res["choice"] == "later"
        browser["signed"] = {"github", "vercel"}
        ctx2 = _ctx([])
        res = await signin.gate(ctx2, WANT)
        assert res["signed_in"] == ["github", "vercel"] and not res["asked"]
        with session() as s:
            assert not s.exec(select(Interaction).where(Interaction.run_id == ctx2.run_id)).first()
        browser["online"] = False
        assert (await signin.gate(_ctx([]), WANT))["offline"]
    loop.run_until_complete(go())


def test_preflight_tells_the_planner_and_connects_clis(loop, browser, clean_vault, monkeypatch):
    connected: list[list[str]] = []

    async def connect_clis(ctx, ids):
        connected.append(ids)
        return [i for i in ids if i == "github"]

    monkeypatch.setattr(signin, "connect_clis", connect_clis)
    browser["signed"] = {"github"}

    async def go():
        ctx = _ctx([])
        task = asyncio.create_task(signin.preflight(ctx, "Launch a waitlist site with a launch post"))
        it = await _card(ctx.run_id)
        assert {s["id"] for s in it.data["services"]} == {"vercel", "x"}
        # why these accounts: the goal's reasons up top, what each account is for on its row
        assert it.data["reason"] == ("You're making a website, so Todd needs somewhere to keep the code and to put "
                                     "the site online. Your goal includes a launch post, so Todd needs the account "
                                     "to post it from.")
        assert "Why: You're making a website" in it.prompt
        assert {s["id"]: s["use"] for s in it.data["services"]} == {"vercel": "Puts the site online",
                                                                    "x": "Posts the launch"}
        resolve_interaction(it.id, decision="deny", answer="Later")
        note = await asyncio.wait_for(task, 5)
        assert connected == [["github"]]
        assert "signed in: GitHub" in note and "CLIs connected" in note
        assert "chose Later for: Vercel, X (Twitter)" in note and "request_signins" in note
        assert await signin.preflight(_ctx([]), "Summarize this article for me") == ""  # nothing needed
    loop.run_until_complete(go())


def test_request_signins_uses_the_same_card(loop, browser, monkeypatch):
    async def connect_clis(ctx, ids):
        return []

    monkeypatch.setattr(signin, "connect_clis", connect_clis)

    async def go():
        ctx = _ctx(["accounts"])
        task = asyncio.create_task(accounts.request_signins.ainvoke({"services": ["Product Hunt"], "reason": "launch"}))
        it = await _card(ctx.run_id)
        assert it.data["kind_hint"] == "signin" and it.data["reason"] == "launch"
        browser["signed"].add("producthunt")
        r = await asyncio.wait_for(task, 5)
        assert r["signed_in"] == ["Product Hunt"] and r["not_signed_in"] == []
    loop.run_until_complete(go())


def test_preflight_asks_for_the_app_store_connect_key_setup(loop, browser, clean_vault, monkeypatch):
    async def connect_clis(ctx, ids):
        return []

    monkeypatch.setattr(signin, "connect_clis", connect_clis)
    browser["signed"] = {"expo", "app_store_connect", "apple_dev", "github"}

    async def go():
        note = await signin.preflight(_ctx([]), "Build an iPhone app and put it on TestFlight")
        assert "App Store Connect is signed in but its keys aren't in the vault yet" in note
        assert 'route "setup"' in note
    loop.run_until_complete(go())


def test_gate_visits_sites_without_a_known_session_cookie(loop, monkeypatch):
    """Most services (Expo, Vercel, Stripe…) only show as signed in after a visit: the card must still close itself."""
    state = {"signed": set(), "verified": set(), "probes": 0}

    async def statuses(ids=None):
        cat = accounts.catalog()
        return {"browser_online": True, "accounts": [
            {"id": i, "name": cat[i].name, "cookie_count": 4, "source": "not checked yet",
             "status": "signed_in" if i in state["verified"] else "unknown"} for i in ids or []]}

    async def verify(i):
        state["probes"] += 1
        if i in state["signed"]:
            state["verified"].add(i)
        return {"id": i}

    monkeypatch.setattr(accounts, "statuses", statuses)
    monkeypatch.setattr(accounts, "verify", verify)
    monkeypatch.setattr(signin, "POLL", 0.05)
    monkeypatch.setattr(signin, "PROBE_EVERY", 0.2)

    async def go():
        ctx = _ctx([])
        task = asyncio.create_task(signin.gate(ctx, [{"id": "expo", "name": "Expo (EAS)", "why": "builds"}]))
        it = await _card(ctx.run_id)
        await asyncio.sleep(0.5)
        assert not task.done() and state["probes"] >= 2  # keeps checking while the human signs in
        state["signed"].add("expo")
        res = await asyncio.wait_for(task, 5)
        assert res["signed_in"] == ["expo"]
        with session() as s:
            assert s.get(Interaction, it.id).status == "approved"
    loop.run_until_complete(go())


def test_squarespace_domains(clean_vault):
    from todd import integrations

    for prompt in ("Build a landing page and point my Squarespace domain at it",
                   "Connect my Google Domains domain to the new site"):
        got = {s["id"]: s for s in needs.detect(prompt)}
        assert "squarespace" in got and got["squarespace"]["use"] == "Manages your domain and its DNS", prompt
    assert accounts.catalog()["squarespace"].category == "Domains & DNS"
    r = integrations.assess("squarespace", {"browser", "sandbox"}, {})
    assert r["route"] == "browser" and "dns-settings" in str(r) and "76.76.21.21" in str(r)
