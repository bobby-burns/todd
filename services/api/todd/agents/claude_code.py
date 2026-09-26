"""Claude Code engine: every agent (planner included) runs as a headless `claude -p` session.

Why: model usage then counts toward the human's own Claude plan. The human signs in to the unmodified Claude Code
CLI through Anthropic's own flow (`claude auth login`); Todd never sees, stores or relays those credentials. It
only starts the CLI as a subprocess.

How it maps onto Todd:

* Todd's tools are served to each session by a small MCP server inside this API (``POST /mcp/{token}``, one
  unguessable token per agent session). Claude Code's built-in tools are all disabled (``--tools ""``) and only
  this server is loaded (``--strict-mcp-config``), so agents can do exactly what their Todd toolsets allow.
* Tool calls go through the same ``execute_tool_calls`` path as the API engine: same timeline events, secret
  scrubbing, spend policy and approvals.
* Pause: a paused agent's next tool call waits at the gate (and so does its next turn).
* Messages from the human or the planner are attached to the agent's next tool result, or sent as a new user
  turn if it's between turns.
* The agent ends by calling Todd's ``finish`` tool. If a turn ends without it, Todd nudges (up to 3 times).
* The session id is fixed per agent, so the planner can be resumed after a restart (``--resume``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from .. import settings
from ..config import config
from ..runtime import RunCancelled, RunContext, set_current_agent
from ..sdk import execute_tool_calls, set_ctx

log = logging.getLogger("todd.claude_code")

SERVER = "todd"
MAX_NUDGES = 3
TOOL_TIMEOUT_MS = 7 * 24 * 3600 * 1000
DEFAULT_MODELS = {"planner": "claude-opus-5-5", "worker": "claude-opus-5-5", "fast": "claude-haiku-4-5-20251001"}
TIER_MAP = {"default": "worker", "worker": "worker", "strong": "planner", "planner": "planner", "fast": "fast",
            "browser": "worker", "coder": "worker"}


# ------------------------------------------------------------------------------------------ settings helpers
def model_for(role: str) -> str:
    cfg = settings.get("claude_code") or {}
    models = {**DEFAULT_MODELS, **(cfg.get("models") or {})}
    return models.get(TIER_MAP.get(role, role)) or models["worker"]


def claude_bin() -> str | None:
    return shutil.which(config.claude_bin) or (config.claude_bin if os.path.isfile(config.claude_bin) else None)


def child_env() -> dict[str, str]:
    """A minimal environment for the CLI. Todd's own secrets (vault key, DB URL, API tokens) are never passed."""
    home = str(config.claude_home)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "CLAUDE_CONFIG_DIR": str(config.claude_config_dir),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "MCP_TOOL_TIMEOUT": str(TOOL_TIMEOUT_MS),  # approvals can take as long as the human needs
        "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT": str(TOOL_TIMEOUT_MS),
        "MCP_TIMEOUT": "30000",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    }
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS",
              "SSL_CERT_FILE", "TZ"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    # Test hook: point the CLI at a scripted API (tests/fake_anthropic.py). Never set in production.
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
        v = os.environ.get(f"TODD_TEST_{k}")
        if v:
            env[k] = v
    return env


async def auth_status() -> dict[str, Any]:
    """`claude auth status` (no model call). Returns {installed, version, loggedIn, authMethod, ...}."""
    exe = claude_bin()
    if not exe:
        return {"installed": False, "loggedIn": False,
                "error": f"Claude Code CLI not found ({config.claude_bin}). It ships in the api image."}
    out: dict[str, Any] = {"installed": True}
    try:
        p = await asyncio.create_subprocess_exec(exe, "--version", stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, env=child_env())
        v, _ = await asyncio.wait_for(p.communicate(), 20)
        out["version"] = v.decode().strip()
        p = await asyncio.create_subprocess_exec(exe, "auth", "status", "--json", stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, env=child_env())
        s, e = await asyncio.wait_for(p.communicate(), 20)
        try:
            out.update(json.loads(s.decode() or "{}"))
        except json.JSONDecodeError:
            out["loggedIn"] = False
            out["detail"] = (s or e).decode()[:500]
    except Exception as ex:  # noqa: BLE001
        out.update(loggedIn=False, error=f"{type(ex).__name__}: {ex}")
    if os.environ.get("TODD_TEST_ANTHROPIC_API_KEY") and not out.get("loggedIn"):
        out.update(loggedIn=True, authMethod="test key")
    return out


def session_exists(session_id: str) -> bool:
    projects = config.claude_config_dir / "projects"
    return projects.is_dir() and any(projects.glob(f"*/{session_id}.jsonl"))


async def ping(role: str = "worker") -> dict[str, Any]:
    """Settings → Test: one tiny prompt through the CLI (uses a little of the plan)."""
    exe = claude_bin()
    if not exe:
        return {"ok": False, "error": "Claude Code CLI not found."}
    model = model_for(role)
    workdir = config.data_dir / "claude-runs" / "_ping"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        p = await asyncio.create_subprocess_exec(
            exe, "-p", "Reply with exactly: pong", "--output-format", "json", "--model", model, "--tools", "",
            "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
            cwd=str(workdir), env=child_env(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), 90)
        data = json.loads(out.decode() or "{}")
        if data.get("is_error"):
            return {"ok": False, "model": model, "error": _explain(str(data.get("result")))}
        return {"ok": True, "model": model, "reply": str(data.get("result", "")).strip()[:200]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "model": model, "error": f"{type(e).__name__}: {e}"[:300]}


# ------------------------------------------------------------------------------------------ MCP bridge
@dataclass
class Session:
    token: str
    ctx: RunContext
    agent_id: str
    tools: dict[str, BaseTool]
    notes: asyncio.Queue | None
    finished: dict[str, Any] | None = None
    tool_calls: int = 0
    max_calls: int = 160
    schemas: list[dict] = field(default_factory=list)
    inflight: set = field(default_factory=set)  # MCP handler tasks still running for this session
    closed: bool = False


_SESSIONS: dict[str, Session] = {}

FINISH_SCHEMA = {
    "name": "finish",
    "description": ("Finish your task with a short recap. Call this once everything is done (or you cannot "
                    "continue). The summary is plain lines: \"Done: …\", \"Outputs: …\" (URLs, IDs, paths, values), "
                    "\"How: …\" (APIs/MCPs/CLIs; browser only if needed), \"Left / needs you: …\"."),
    "inputSchema": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "the recap"},
        "success": {"type": "boolean", "description": "whether the task was fully achieved", "default": True}},
        "required": ["summary"]},
}


def _schema(t: BaseTool) -> dict:
    fn = convert_to_openai_tool(t)["function"]
    params = fn.get("parameters") or {"type": "object", "properties": {}}
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return {"name": t.name, "description": (fn.get("description") or t.description or "")[:4000], "inputSchema": params}


def register(ctx: RunContext, agent_id: str, tools: list[BaseTool], notes: asyncio.Queue | None,
             max_calls: int = 160) -> Session:
    token = secrets.token_urlsafe(32)
    tool_map = {t.name: t for t in tools if t.name != "finish"}
    sess = Session(token=token, ctx=ctx, agent_id=agent_id, tools=tool_map, notes=notes, max_calls=max_calls,
                   schemas=[_schema(t) for t in tool_map.values()] + [FINISH_SCHEMA])
    _SESSIONS[token] = sess
    return sess


async def unregister(token: str) -> None:
    """Close a session: no new tool calls, and any still running (e.g. waiting on a lock or an approval) are
    cancelled so nothing keeps acting for an agent that has ended."""
    sess = _SESSIONS.pop(token, None)
    if sess is None:
        return
    sess.closed = True
    tasks = [t for t in sess.inflight if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=15)


def _drain(q: asyncio.Queue | None) -> list[str]:
    out: list[str] = []
    while q is not None and not q.empty():
        out.append(q.get_nowait())
    return out


def _rpc(id_: Any, result: Any = None, error: dict | None = None) -> dict:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


async def handle_mcp(token: str, msg: dict) -> dict | None:
    """One JSON-RPC message from a Claude Code session. Returns the response (None for notifications)."""
    sess = _SESSIONS.get(token)
    if "id" not in msg:
        return None
    mid, method = msg["id"], msg.get("method")
    if sess is None:
        return _rpc(mid, error={"code": -32001, "message": "unknown or finished session"})
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return _rpc(mid, {"protocolVersion": pv, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": SERVER, "version": "0.4"}})
    if method == "ping":
        return _rpc(mid, {})
    if method == "tools/list":
        return _rpc(mid, {"tools": sess.schemas})
    if method == "tools/call":
        params = msg.get("params") or {}
        task = asyncio.current_task()
        if sess.closed:
            return _rpc(mid, error={"code": -32001, "message": "session closed"})
        sess.inflight.add(task)
        try:
            return _rpc(mid, await _call(sess, params.get("name", ""), params.get("arguments") or {}))
        except asyncio.CancelledError:
            return _rpc(mid, {"content": [{"type": "text", "text": "Stopped: this agent has ended."}], "isError": True})
        finally:
            sess.inflight.discard(task)
    return _rpc(mid, error={"code": -32601, "message": f"method not found: {method}"})


async def _call(sess: Session, name: str, args: dict) -> dict:
    ctx = sess.ctx
    set_ctx(ctx)
    set_current_agent(sess.agent_id)
    try:
        await ctx.wait_if_paused(sess.agent_id)  # a paused agent's next action waits here
    except RunCancelled:
        pass
    if ctx.cancelled:
        return {"content": [{"type": "text", "text": "The run was cancelled. Stop now."}], "isError": True}
    sess.tool_calls += 1
    if sess.finished is not None:
        return {"content": [{"type": "text", "text": "You already finished. Don't call more tools."}], "isError": True}
    if name == "finish":
        blocked = ctx.finish_blocker(sess.agent_id)
        if blocked:
            ctx.emit(sess.agent_id, "status", "Can't finish yet: agents are still running")
            return {"content": [{"type": "text", "text": blocked}], "isError": True}
        sess.finished ={"summary": str(args.get("summary", "")), "success": bool(args.get("success", True))}
        return {"content": [{"type": "text", "text": "Finished. Don't call any more tools; reply with one short line."}],
                "isError": False}
    if sess.tool_calls > sess.max_calls:
        return {"content": [{"type": "text", "text": "Step limit reached. Call finish now with what you have."}],
                "isError": True}
    call_id = uuid.uuid4().hex[:12]
    try:
        [res] = await execute_tool_calls(ctx, sess.tools, [{"name": name, "args": args, "id": call_id}], sess.agent_id)
    except (RunCancelled, asyncio.CancelledError):
        return {"content": [{"type": "text", "text": "Stopped: the run or this agent was cancelled. Don't continue."}],
                "isError": True}
    text = res.content if isinstance(res.content, str) else json.dumps(res.content)
    notes = _drain(sess.notes)
    if notes:
        text += "\n\n---\nNew message(s) while you work (adapt your plan):\n" + "\n".join(notes)
    content: list[dict] = [{"type": "text", "text": text}]
    image = ctx.pop_image(sess.agent_id)
    if image:
        content.append({"type": "image", "data": image, "mimeType": "image/png"})
    return {"content": content, "isError": getattr(res, "status", "success") == "error"}


# ------------------------------------------------------------------------------------------ runner
class ClaudeCodeError(RuntimeError):
    pass


def _user(text: str) -> bytes:
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n").encode()


async def run_agent(
    ctx: RunContext,
    *,
    role: str,
    agent_id: str,
    system_prompt: str,
    tools: list[BaseTool],
    first_message: str,
    notes: asyncio.Queue | None = None,
    session_id: str | None = None,
    resume: bool = False,
    max_calls: int | None = None,
) -> dict[str, Any]:
    """Run one agent to completion as a Claude Code session. Returns {done, success, summary, session_id}."""
    exe = claude_bin()
    if not exe:
        raise ClaudeCodeError("Claude Code CLI not found. Switch Settings → Engine to API keys, or rebuild the api image.")
    sess = register(ctx, agent_id, tools, notes, max_calls=max_calls or config.code_max_steps * 2)
    session_id = session_id or str(uuid.uuid4())
    workdir = config.data_dir / "claude-runs" / ctx.run_id / agent_id
    workdir.mkdir(parents=True, exist_ok=True)
    model = model_for(role)
    mcp = {"mcpServers": {SERVER: {"type": "http", "url": f"{config.internal_url}/mcp/{sess.token}",
                                   "timeout": TOOL_TIMEOUT_MS}}}
    args = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", model, "--system-prompt", system_prompt, "--tools", "", "--strict-mcp-config",
            "--mcp-config", json.dumps(mcp), "--allowedTools", f"mcp__{SERVER}", "--permission-mode", "dontAsk",
            "--setting-sources", ""]
    thinking = settings.get("thinking") or {}
    if thinking.get("enabled"):
        args += ["--effort", str(thinking.get("effort", "medium"))]
    # On resume, render the system prompt fresh (integrations/accounts may have changed since the run started).
    args += ["--resume", session_id, "--system-prompt-snapshot", "off"] if resume else ["--session-id", session_id]

    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(workdir), env=child_env(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024, start_new_session=True)
    stderr_tail: list[str] = []

    async def read_stderr() -> None:
        assert proc.stderr
        async for line in proc.stderr:
            stderr_tail.append(line.decode(errors="replace").rstrip())
            del stderr_tail[:-40]

    err_task = asyncio.create_task(read_stderr())
    nudges = 0
    calls_at_last_result = 0
    last_text = ""
    outcome: dict[str, Any] = {"done": False, "success": False, "summary": "", "session_id": session_id}
    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(_user(first_message))
        await proc.stdin.drain()
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            et = ev.get("type")
            if et == "system" and ev.get("subtype") == "api_retry":
                status, err = ev.get("error_status"), str(ev.get("error") or "")
                if status in (401, 403) or "auth" in err:
                    raise ClaudeCodeError(_explain(f"not logged in ({err or status})"))
                if status == 429 or "rate" in err or "overloaded" in err:
                    secs = int((ev.get("retry_delay_ms") or 0) / 1000)
                    ctx.emit(agent_id, "status", f"Claude is rate-limiting your plan; retrying in {secs}s "
                                                 f"(attempt {ev.get('attempt')}/{ev.get('max_retries')})")
            elif et == "system" and ev.get("subtype") == "init":
                bad = [s for s in ev.get("mcp_servers", []) if s.get("name") == SERVER and s.get("status") != "connected"]
                if bad:
                    raise ClaudeCodeError(f"Claude Code couldn't reach Todd's tools ({bad[0].get('status')}).")
            elif et == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    bt = block.get("type")
                    if bt == "thinking" and block.get("thinking", "").strip():
                        ctx.emit(agent_id, "thinking", block["thinking"].strip())
                    elif bt == "text" and block.get("text", "").strip():
                        last_text = block["text"].strip()
                        if not sess.finished:  # the closing line after finish is noise
                            ctx.emit(agent_id, "thought", last_text)
            elif et == "result":
                if ev.get("is_error") and not sess.finished:
                    msg = str(ev.get("result") or ev.get("subtype") or "error")
                    raise ClaudeCodeError(_explain(msg))
                if sess.finished:
                    outcome.update(done=True, **sess.finished)
                    break
                if ctx.cancelled:
                    raise RunCancelled()
                await ctx.wait_if_paused(agent_id)
                if sess.tool_calls > calls_at_last_result:
                    nudges = 0  # it did work this turn; only consecutive idle turns count
                calls_at_last_result = sess.tool_calls
                nudges += 1
                if nudges > MAX_NUDGES:
                    outcome.update(done=True, success=False, summary=last_text or "Stopped without finishing.")
                    break
                pending = _drain(notes)
                text = ("New message(s):\n" + "\n".join(pending) + "\n\n") if pending else ""
                text += "Continue working using your tools. When everything is done, call `finish` with a summary."
                proc.stdin.write(_user(text))
                await proc.stdin.drain()
        else:
            if not sess.finished:
                code = await proc.wait()
                raise ClaudeCodeError(_explain("\n".join(stderr_tail[-8:]) or f"Claude Code exited ({code})."))
            outcome.update(done=True, **sess.finished)
        return outcome
    finally:
        async def cleanup() -> None:
            from ..tools.browser_direct import release

            await _stop(proc, graceful=bool(outcome.get("done")))
            await unregister(sess.token)
            err_task.cancel()
            await release(ctx, agent_id, success=False, result="agent ended without browser_done")

        # shielded: a second cancel while cleaning up must not leave the process, lock or session behind
        await asyncio.shield(asyncio.ensure_future(cleanup()))


def _explain(msg: str) -> str:
    low = msg.lower()
    if "login" in low or "not logged in" in low or "invalid api key" in low or "authenticat" in low or "oauth" in low:
        return ("Claude Code isn't signed in. Run `docker compose exec -it api claude auth login` and sign in with "
                f"your Claude account, then resume the run. ({msg[:200]})")
    if "usage limit" in low or "rate limit" in low or "limit reached" in low:
        return f"Your Claude plan's usage limit was reached. Resume the run when it resets. ({msg[:200]})"
    return f"Claude Code: {msg[:500]}"


async def _stop(proc: asyncio.subprocess.Process, graceful: bool = True) -> None:
    """Finished normally: close stdin and let the CLI save and exit. Cancelled/failed: kill it right away so it
    can't keep acting (or write more turns into the session transcript)."""
    if proc.returncode is not None:
        return
    try:
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        if graceful:
            await asyncio.wait_for(proc.wait(), 5)
            return
    except (asyncio.TimeoutError, Exception):
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), 3)
            return
        except asyncio.TimeoutError:
            continue
