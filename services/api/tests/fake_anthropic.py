"""A scripted stand-in for the Anthropic Messages API, for end-to-end tests of the Claude Code engine.

The real `claude` CLI is pointed at it with ANTHROPIC_BASE_URL. Scripts are registered over HTTP:

    POST /_scripts {"key": "<text found in the system prompt>", "steps": [step, ...]}

A step is {"blocks": [...], "delay": seconds}. Blocks are {"type": "text", "text"}, {"type": "thinking", "thinking"}
or {"type": "tool_use", "name": "<tool>", "input": {...}} (MCP tools are addressed as mcp__todd__<tool>).
The step used is the number of assistant turns already in the request, so a script replays deterministically.
Requests that match no script (the CLI's own side calls) get a short text reply.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
SCRIPTS: dict[str, list[dict]] = {}
LOG: list[dict] = []
_ids = itertools.count(1)


@app.post("/_scripts")
async def add_script(body: dict) -> dict:
    SCRIPTS[body["key"]] = body["steps"]
    return {"ok": True, "keys": list(SCRIPTS)}


@app.delete("/_scripts")
async def clear_scripts() -> dict:
    SCRIPTS.clear()
    LOG.clear()
    return {"ok": True}


@app.get("/_log")
async def get_log() -> list[dict]:
    return LOG


def _system_text(body: dict) -> str:
    s = body.get("system") or ""
    if isinstance(s, list):
        return "\n".join(b.get("text", "") for b in s if isinstance(b, dict))
    return str(s)


def _first_user_text(body: dict) -> str:
    for m in body.get("messages", []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _pick(body: dict) -> tuple[str | None, dict]:
    # keys match the system prompt (agent identity) or, failing that, the first user message (e.g. the run prompt)
    system = _system_text(body) + "\n" + _first_user_text(body)
    # longest key first so "Agent: X" style keys don't collide with shorter ones
    for key in sorted(SCRIPTS, key=len, reverse=True):
        if key in system:
            turns = sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")
            steps = SCRIPTS[key]
            if turns < len(steps):
                return key, steps[turns]
            return key, {"blocks": [{"type": "text", "text": "Done."}]}
    return None, {"blocks": [{"type": "text", "text": "ok"}]}


def _content(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        if b["type"] == "tool_use":
            out.append({"type": "tool_use", "id": f"toolu_{next(_ids):06d}", "name": b["name"], "input": b.get("input", {})})
        elif b["type"] == "thinking":
            out.append({"type": "thinking", "thinking": b["thinking"], "signature": "sig"})
        else:
            out.append({"type": "text", "text": b["text"]})
    return out


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/v1/messages")
async def messages(request: Request):
    if request.headers.get("x-api-key") == "sk-bad":
        return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
                            status_code=401)
    body = await request.json()
    key, step = _pick(body)
    if step.get("error") == "auth":
        return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
                            status_code=401)
    tools = [t.get("name") for t in body.get("tools", [])]
    LOG.append({"key": key, "model": body.get("model"), "tools": tools, "n_messages": len(body.get("messages", [])),
                "roles": [m.get("role") for m in body.get("messages", [])],
                "last": body.get("messages", [{}])[-1] if body.get("messages") else None})
    if step.get("delay"):
        await asyncio.sleep(float(step["delay"]))
    content = _content(step.get("blocks", []))
    stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
    msg: dict[str, Any] = {"id": f"msg_{next(_ids):06d}", "type": "message", "role": "assistant", "model": body.get("model"),
                           "content": content, "stop_reason": stop, "stop_sequence": None,
                           "usage": {"input_tokens": 100, "output_tokens": 20}}
    if not body.get("stream"):
        return JSONResponse(msg)

    async def gen():
        start = dict(msg, content=[], stop_reason=None, usage={"input_tokens": 100, "output_tokens": 1})
        yield _sse("message_start", {"type": "message_start", "message": start})
        for i, c in enumerate(content):
            if c["type"] == "text":
                yield _sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": c["text"]}})
            elif c["type"] == "thinking":
                yield _sse("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": c["thinking"]}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "sig"}})
            else:
                yield _sse("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "tool_use", "id": c["id"], "name": c["name"], "input": {}}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                   "delta": {"type": "input_json_delta", "partial_json": json.dumps(c["input"])}})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})
        yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": 20}})
        yield _sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    return {"input_tokens": 100}


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def other(path: str, request: Request):
    LOG.append({"other": path})
    return JSONResponse({})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8111, log_level="warning")
