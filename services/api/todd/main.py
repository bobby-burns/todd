"""Todd API: runs, live events (SSE), approvals, settings, vault, prompts, tools, ledger."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import accounts, events, llm, prompts, registry, settings, vault
from .config import config
from .db import AgentInstance, Event, Interaction, LedgerEntry, Run, init_db, select, session
from .orchestrator import integrations_summary, manager
from .runtime import resolve_interaction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    registry.load_plugins()
    await manager.startup()
    yield
    await manager.shutdown()


app = FastAPI(title="Todd", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=config.cors_origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    if config.api_token and request.url.path.startswith("/api/") and request.url.path != "/api/health":
        supplied = request.headers.get("x-todd-token") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.strip(), config.api_token):
            return JSONResponse({"detail": "missing or invalid API token"}, status_code=401)
    return await call_next(request)


# ------------------------------------------------------------------------------------------ meta
@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/meta")
def meta() -> dict:
    return {"browser_live_url": config.browser_live_url, "version": app.version,
            "integrations": integrations_summary()}


# ------------------------------------------------------------------------------------------ runs
class NewRun(BaseModel):
    prompt: str = Field(min_length=1)
    title: str | None = None
    budget_usd: float | None = None


def _planner_model(r: Run) -> str:
    if _run_dict(r).get("engine") == "claude_code":
        from .agents import claude_code

        return claude_code.model_for("planner")
    return llm.model_for("planner").model


def _run_dict(r: Run) -> dict:
    d = r.model_dump()
    d["active"] = manager.is_active(r.id)
    ctx = manager.contexts.get(r.id)
    d["paused_agents"] = sorted(a for a, g in (ctx.gates.items() if ctx and d["active"] else []) if g.is_paused)
    with session() as s:
        d["pending_interactions"] = len(s.exec(select(Interaction).where(
            Interaction.run_id == r.id, Interaction.status == "pending")).all())
        eng = getattr(ctx, "engine", None) if ctx else None
    if not eng:
        from .orchestrator import run_engine

        eng = run_engine(r.id)
    d["engine"] = eng or "api"
    return d


@app.get("/api/runs")
def list_runs(limit: int = 50) -> list[dict]:
    with session() as s:
        rows = s.exec(select(Run).order_by(Run.created_at.desc()).limit(limit)).all()  # type: ignore
    return [_run_dict(r) for r in rows]


@app.post("/api/runs")
async def create_run(body: NewRun) -> dict:
    budget = body.budget_usd if body.budget_usd is not None else \
        float(settings.get("spend_policy")["default_run_budget_usd"])
    title = body.title or body.prompt.strip().split("\n")[0][:80]
    with session() as s:
        run = Run(title=title, prompt=body.prompt, budget_usd=budget)
        s.add(run)
        s.commit()
        s.refresh(run)
    manager.start(run.id)
    return _run_dict(run)


def _get_run(run_id: str) -> Run:
    with session() as s:
        run = s.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return _run_dict(_get_run(run_id))


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    _get_run(run_id)
    return {"cancelled": manager.cancel(run_id)}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str) -> dict:
    run = _get_run(run_id)
    if manager.is_active(run_id):
        raise HTTPException(409, "run is already active")
    if run.status == "succeeded":
        raise HTTPException(409, "run already succeeded")
    manager.start(run_id, resume=True)
    return {"resumed": True}


class Note(BaseModel):
    text: str = Field(min_length=1)
    agent_id: str = "planner"


@app.post("/api/runs/{run_id}/message")
async def message_run(run_id: str, body: Note) -> dict:
    """Steer a running run: the planner sees the message on its next turn."""
    _get_run(run_id)
    if not manager.note(run_id, body.text, body.agent_id):
        raise HTTPException(409, "run or agent is not active")
    return {"queued": True}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, after: int = 0):
    """Server-sent events: replays stored events after `after`, then streams live ones."""
    _get_run(run_id)
    q = events.subscribe(run_id)

    async def gen():
        try:
            with session() as s:
                rows = s.exec(select(Event).where(Event.run_id == run_id, Event.id > after)  # type: ignore
                              .order_by(Event.id)).all()
            last = after
            for ev in rows:
                last = ev.id or last
                yield {"event": "event", "id": str(ev.id), "data": json.dumps(events.serialize(ev))}
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                if payload["id"] <= last:
                    continue
                last = payload["id"]
                yield {"event": "event", "id": str(payload["id"]), "data": json.dumps(payload)}
        finally:
            events.unsubscribe(q, run_id)

    return EventSourceResponse(gen())


@app.get("/api/runs/{run_id}/interactions")
def run_interactions(run_id: str, pending_only: bool = False) -> list[dict]:
    with session() as s:
        stmt = select(Interaction).where(Interaction.run_id == run_id)
        if pending_only:
            stmt = stmt.where(Interaction.status == "pending")
        return [i.model_dump() for i in s.exec(stmt.order_by(Interaction.created_at)).all()]  # type: ignore


@app.get("/api/interactions")
def pending_interactions() -> list[dict]:
    with session() as s:
        rows = s.exec(select(Interaction).where(Interaction.status == "pending")
                      .order_by(Interaction.created_at)).all()  # type: ignore
    return [i.model_dump() for i in rows]


class Resolve(BaseModel):
    decision: str | None = None  # approve | deny (approvals/spend)
    answer: str | None = None  # free text (questions, or a note on approvals)


@app.post("/api/interactions/{interaction_id}")
async def resolve(interaction_id: str, body: Resolve) -> dict:
    it = resolve_interaction(interaction_id, decision=body.decision, answer=body.answer)
    if not it:
        raise HTTPException(404, "interaction not found")
    return it.model_dump()


@app.get("/api/screens/{run_id}/{name}")
def screenshot(run_id: str, name: str):
    path = (config.data_dir / "screens" / run_id / name).resolve()
    if not str(path).startswith(str((config.data_dir / "screens").resolve())) or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


# ------------------------------------------------------------------------------------------ ledger
@app.get("/api/ledger")
def ledger(run_id: str | None = None) -> dict:
    with session() as s:
        stmt = select(LedgerEntry)
        if run_id:
            stmt = stmt.where(LedgerEntry.run_id == run_id)
        rows = s.exec(stmt.order_by(LedgerEntry.ts.desc())).all()  # type: ignore
    entries = [r.model_dump() for r in rows]
    total = sum(r["amount_usd"] for r in entries if r["status"] in ("authorized", "completed", "needs_review"))
    return {"entries": entries, "total_usd": round(total, 2)}


# ------------------------------------------------------------------------------------------ settings
@app.get("/api/settings")
def get_settings() -> dict:
    return settings.get_all()


@app.patch("/api/settings")
def patch_settings(patch: dict[str, Any]) -> dict:
    return settings.update(patch)


@app.post("/api/settings/test-model/{role}")
async def test_model(role: str) -> dict:
    spec = llm.model_for(role)
    try:
        reply = await asyncio.wait_for(llm.ping(role), timeout=60)
        return {"ok": True, "model": spec.model, "reply": reply}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "model": spec.model, "error": f"{type(e).__name__}: {str(e)[:500]}"}


# ------------------------------------------------------------------------------------------ Claude Code engine
@app.get("/api/claude-code/status")
async def claude_code_status() -> dict:
    from .agents import claude_code

    st = await claude_code.auth_status()
    st["engine"] = settings.get("engine")
    st["models"] = {k: claude_code.model_for(k) for k in ("planner", "worker", "fast")}
    st["login_command"] = "docker compose exec -it api claude auth login"
    return st


@app.post("/api/claude-code/test")
async def claude_code_test() -> dict:
    from .agents import claude_code

    return await claude_code.ping()


@app.post("/mcp/{token}")
async def mcp_endpoint(token: str, request: Request):
    """MCP (streamable HTTP, JSON responses) for Claude Code sessions started by this API. Loopback only; the path
    token is per agent session and unguessable."""
    from fastapi.responses import Response

    from .agents import claude_code

    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    body = await request.json()
    if isinstance(body, list):
        out = [r for r in [await claude_code.handle_mcp(token, m) for m in body] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    res = await claude_code.handle_mcp(token, body)
    return JSONResponse(res) if res is not None else Response(status_code=202)


@app.get("/mcp/{token}")
async def mcp_stream(token: str):
    from fastapi.responses import Response

    return Response(status_code=405)  # no server-initiated stream


@app.delete("/mcp/{token}")
async def mcp_close(token: str):
    return {"ok": True}


@app.get("/api/integrations/check")
async def check_integrations() -> dict:
    out: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=20) as c:
        tok = vault.get_secret("VERCEL_TOKEN")
        if tok:
            r = await c.get("https://api.vercel.com/v2/user", headers={"Authorization": f"Bearer {tok}"})
            out["vercel"] = {"ok": r.status_code == 200,
                             "user": r.json().get("user", {}).get("username") if r.status_code == 200 else None}
        else:
            out["vercel"] = {"ok": False, "error": "no token"}
        tok = vault.get_secret("GITHUB_TOKEN")
        if tok:
            r = await c.get("https://api.github.com/user", headers={"Authorization": f"Bearer {tok}"})
            out["github"] = {"ok": r.status_code == 200, "user": r.json().get("login") if r.status_code == 200 else None}
        else:
            out["github"] = {"ok": False, "error": "no token"}
    return out


# ------------------------------------------------------------------------------------------ vault
class SecretBody(BaseModel):
    value: str = Field(min_length=1)


@app.get("/api/secrets")
def secrets_list() -> list[dict]:
    return vault.list_secrets()


@app.put("/api/secrets/{name}")
def secrets_put(name: str, body: SecretBody) -> dict:
    vault.set_secret(name.strip().upper(), body.value.strip())
    return {"ok": True}


@app.delete("/api/secrets/{name}")
def secrets_delete(name: str) -> dict:
    return {"deleted": vault.delete_secret(name)}


# ------------------------------------------------------------------------------------------ prompts & tools
@app.get("/api/prompts")
def prompts_get() -> dict:
    return prompts.describe()


class PromptBody(BaseModel):
    content: str = ""  # empty resets to file/default


@app.put("/api/prompts/{name}")
def prompts_put(name: str, body: PromptBody) -> dict:
    if name not in prompts.NAMES:
        raise HTTPException(404, "unknown prompt")
    settings.update({"prompts": {name: body.content}})
    return prompts.describe()[name]


@app.get("/api/tools")
async def tools_catalog(include_mcp: bool = False) -> dict:
    extra, errors = (await registry.load_mcp_toolsets()) if include_mcp else ({}, [])
    cat = registry.catalog(extra)
    cat["mcp_errors"] = errors
    return cat


@app.post("/api/tools/reload")
def tools_reload() -> dict:
    registry.load_plugins()
    return registry.catalog()


# ------------------------------------------------------------------------------------------ agents
@app.get("/api/runs/{run_id}/agents")
def run_agents(run_id: str) -> list[dict]:
    """The planner plus every agent it spawned, with status (for the windows view)."""
    run = _get_run(run_id)
    with session() as s:
        rows = s.exec(select(AgentInstance).where(AgentInstance.run_id == run_id)
                      .order_by(AgentInstance.created_at)).all()  # type: ignore[arg-type]
        pending = {i.agent for i in s.exec(select(Interaction).where(
            Interaction.run_id == run_id, Interaction.status == "pending")).all()}
    # The run is "waiting" if *any* agent needs the human; the planner only "needs you" for its own requests.
    planner_status = "waiting" if "planner" in pending else ("running" if run.status == "waiting" else run.status)
    if manager.is_paused(run_id, "planner"):
        planner_status = "paused"
    out = [{"id": "planner", "name": "Planner", "status": planner_status, "toolsets": [], "model_tier": "planner",
            "model": _planner_model(run), "summary": run.summary, "llm_cost_usd": None,
            "task": run.prompt, "instructions": "", "background": False, "created_at": run.created_at}]
    for r in rows:
        d = r.model_dump()
        if r.status == "running" and r.id in pending:
            d["status"] = "waiting"
        out.append(d)
    return out


@app.post("/api/runs/{run_id}/agents/{agent_id}/pause")
async def pause_run_agent(run_id: str, agent_id: str) -> dict:
    """Pause one agent (or "planner"). It stops at its next step; a browser task pauses between browser steps."""
    _get_run(run_id)
    if not manager.pause(run_id, agent_id):
        raise HTTPException(409, "agent is not running or already paused")
    return {"paused": True}


@app.post("/api/runs/{run_id}/agents/{agent_id}/resume")
async def resume_run_agent(run_id: str, agent_id: str) -> dict:
    _get_run(run_id)
    if not manager.pause(run_id, agent_id, resume=True):
        raise HTTPException(409, "agent is not paused")
    return {"resumed": True}


@app.post("/api/runs/{run_id}/pause-all")
async def pause_all(run_id: str) -> dict:
    _get_run(run_id)
    return {"paused": manager.pause(run_id)}


@app.post("/api/runs/{run_id}/resume-all")
async def resume_all(run_id: str) -> dict:
    _get_run(run_id)
    return {"resumed": manager.pause(run_id, resume=True)}


@app.post("/api/runs/{run_id}/agents/{agent_id}/cancel")
async def cancel_run_agent(run_id: str, agent_id: str) -> dict:
    _get_run(run_id)
    if not manager.cancel_agent(run_id, agent_id):
        raise HTTPException(409, "agent is not running")
    return {"cancelled": True}


# ------------------------------------------------------------------------------------------ accounts
@app.get("/api/accounts")
async def accounts_list(selected_only: bool = False) -> dict:
    ids = (settings.get("accounts_selected") or []) if selected_only else None
    st = await accounts.statuses(ids)
    st["categories"] = list(dict.fromkeys(a["category"] for a in st["accounts"]))
    return st


@app.get("/api/accounts/{service_id}")
async def account_status(service_id: str) -> dict:
    st = await accounts.statuses([service_id])
    if not st["accounts"]:
        raise HTTPException(404, "unknown service")
    return {"browser_online": st["browser_online"], **st["accounts"][0]}


async def _browser_call(coro):
    try:
        return await coro
    except KeyError:
        raise HTTPException(404, "unknown service")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"browser unavailable: {e}")


@app.post("/api/accounts/{service_id}/open")
async def account_open(service_id: str) -> dict:
    return {"target": await _browser_call(accounts.open_login(service_id))}


@app.post("/api/accounts/{service_id}/verify")
async def account_verify(service_id: str) -> dict:
    return await _browser_call(accounts.verify(service_id))


@app.post("/api/accounts/{service_id}/signout")
async def account_signout(service_id: str) -> dict:
    return {"cookies_removed": await _browser_call(accounts.sign_out(service_id))}


class MarkBody(BaseModel):
    signed_in: bool


@app.post("/api/accounts/{service_id}/mark")
def account_mark(service_id: str, body: MarkBody) -> dict:
    if service_id not in accounts.catalog():
        raise HTTPException(404, "unknown service")
    accounts.mark(service_id, body.signed_in)
    return {"ok": True}


class Selection(BaseModel):
    ids: list[str]


@app.put("/api/accounts-selection")
def accounts_select(body: Selection) -> dict:
    cat = accounts.catalog()
    ids = [i for i in dict.fromkeys(body.ids) if i in cat]
    settings.update({"accounts_selected": ids})
    return {"selected": ids}


class CustomAccount(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    login: str = Field(pattern=r"^https?://")
    check: str | None = None
    cookies: list[str] = []


@app.post("/api/accounts-custom")
def accounts_add_custom(body: CustomAccount) -> dict:
    import re as _re
    from urllib.parse import urlparse

    host = (urlparse(body.login).hostname or "").removeprefix("www.")
    if not host:
        raise HTTPException(400, "invalid login URL")
    sid = "custom_" + _re.sub(r"[^a-z0-9]+", "_", body.name.lower()).strip("_")
    custom = [c for c in (settings.get("accounts_custom") or []) if c.get("id") != sid]
    custom.append({"id": sid, "name": body.name, "login": body.login, "check": body.check or body.login,
                   "domains": [host], "cookies": body.cookies})
    settings.update({"accounts_custom": custom})
    sel = list(settings.get("accounts_selected") or [])
    if sid not in sel:
        settings.update({"accounts_selected": sel + [sid]})
    return {"id": sid}


@app.delete("/api/accounts-custom/{service_id}")
def accounts_delete_custom(service_id: str) -> dict:
    custom = [c for c in (settings.get("accounts_custom") or []) if c.get("id") != service_id]
    settings.update({"accounts_custom": custom,
                     "accounts_selected": [i for i in (settings.get("accounts_selected") or []) if i != service_id]})
    return {"deleted": True}


# ------------------------------------------------------------------------------------------ integrations
@app.get("/api/integrations/catalog")
def integrations_catalog() -> list[dict]:
    """Known services with their API, CLI and official MCP server (for 'add MCP server' suggestions)."""
    from . import integrations

    return integrations.catalog()


# ------------------------------------------------------------------------------------------ onboarding
@app.get("/api/onboarding")
async def onboarding() -> dict:
    cfg = settings.get_all()
    planner = llm.model_for("planner")
    model_ok = bool(planner.api_key) or planner.provider in llm.LOCAL_PROVIDERS or \
        bool(cfg.get("openai_compatible_base_url"))
    contact = cfg["registrant_contact"]
    selected = cfg.get("accounts_selected") or []
    signed = 0
    browser_online = False
    if selected:
        st = await accounts.statuses(selected)
        browser_online = st["browser_online"]
        signed = sum(1 for a in st["accounts"] if a["status"] == "signed_in")
    else:
        from . import cdp
        browser_online = await cdp.online()
    if cfg.get("engine") == "claude_code":
        from .agents import claude_code

        st_cc = await claude_code.auth_status()
        model_ok = bool(st_cc.get("loggedIn"))
        model_detail = f"Claude plan · {claude_code.model_for('planner')}" if model_ok else "Claude Code not signed in"
    else:
        model_detail = planner.model
    steps = {
        "model": {"done": model_ok, "detail": model_detail},
        "integrations": {"done": vault.has_secret("VERCEL_TOKEN") or vault.has_secret("GITHUB_TOKEN"),
                         "vercel": vault.has_secret("VERCEL_TOKEN"), "github": vault.has_secret("GITHUB_TOKEN")},
        "spending": {"done": bool(contact.get("email")), "registrant": bool(contact.get("email"))},
        "accounts": {"done": bool(selected) and signed == len(selected), "selected": len(selected),
                     "signed_in": signed, "browser_online": browser_online},
    }
    return {"completed": bool(cfg.get("onboarding_completed")), "steps": steps}


@app.post("/api/onboarding/complete")
def onboarding_complete() -> dict:
    settings.update({"onboarding_completed": True})
    return {"ok": True}
