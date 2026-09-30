"""The run's files, read-only (the dashboard's Files view): the real sandbox server on a temp folder, reached through
the API, with vault values, `.env` values and private keys kept out of sight, and nothing outside the run's folder."""

from __future__ import annotations

import base64
import importlib.util
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from todd import vault, workspace
from todd.config import config
from todd.sdk import ToolError
from todd.tools import sandbox

from .helpers import new_run

SERVER = Path(__file__).resolve().parents[2] / "sandbox" / "server.py"
SECRET = "sk_live_ws_0123456789abcdef"


@pytest.fixture
def box(tmp_path, monkeypatch):
    """The sandbox's file server on tmp_path, standing in for the sandbox container."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    spec = importlib.util.spec_from_file_location("todd_sandbox_server_test", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    client = TestClient(mod.app)
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))

    async def call(path, payload, timeout=120):
        r = client.post(path, json=payload, headers={"X-Sandbox-Token": "t"})
        if r.status_code >= 400:
            raise ToolError(f"sandbox {path} -> {r.status_code}: {r.text[:1000]}")
        return r.json()

    monkeypatch.setattr(sandbox, "call", call)
    vault.set_secret("WS_TEST_KEY", SECRET)
    yield tmp_path
    vault.delete_secret("WS_TEST_KEY")


def _project(root: Path, run_id: str) -> Path:
    p = root / run_id
    (p / "app" / "src").mkdir(parents=True)
    (p / "app" / "src" / "index.ts").write_text(f"const key = '{SECRET}';\nexport default key;\n")
    (p / "app" / "README.md").write_text("# Water tracker\n")
    (p / "app" / "node_modules" / "left-pad").mkdir(parents=True)
    (p / "app" / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1")
    (p / "app" / ".env").write_text(f"API_URL=https://api.example.com\nSTRIPE_KEY={SECRET}\n# comment\n")
    (p / "app" / "AuthKey.p8").write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    (p / "app" / "notes.txt").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n")
    (p / "app" / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
    (p / "app" / "blob.bin").write_bytes(b"\0\1\2" * 10)
    return p


def test_tree_and_view(loop, box):
    rid = new_run("files")
    _project(box, rid)
    other = box / "someone-else"
    other.mkdir()
    (other / "secret.txt").write_text("not yours")
    os.symlink(other, box / rid / "app" / "peek")  # a symlinked folder isn't followed
    os.symlink(other / "secret.txt", box / rid / "app" / "peek.txt")

    async def go():
        t = await workspace.tree(rid)
        paths = {e["path"]: e for e in t["entries"]}
        assert t["exists"] and not t["truncated"]
        assert paths["app/src/index.ts"]["size"] > 0 and paths["app/src"]["dir"]
        assert paths["app/node_modules"]["skipped"] and "app/node_modules/left-pad/index.js" not in paths
        assert paths["app/peek"]["skipped"] and "app/peek/secret.txt" not in paths

        v = await workspace.view(rid, "app/src/index.ts")
        assert v["kind"] == "text" and SECRET not in v["content"] and "***" in v["content"]

        env = await workspace.view(rid, "app/.env")
        assert env["env"] and "https://api.example.com" not in env["content"] and "# comment" in env["content"]
        env = await workspace.view(rid, "app/.env", reveal=True)
        assert "API_URL=https://api.example.com" in env["content"] and SECRET not in env["content"]

        for hidden in ("app/AuthKey.p8", "app/notes.txt"):
            v = await workspace.view(rid, hidden)
            assert v["kind"] == "hidden" and "abc" not in str(v)
        assert (await workspace.view(rid, "app/icon.png"))["kind"] == "image"
        assert (await workspace.view(rid, "app/blob.bin"))["kind"] == "binary"

        name, data = await workspace.download(rid, "app/src/index.ts")
        assert name == "index.ts" and SECRET.encode() not in data
        with pytest.raises(ToolError):
            await workspace.download(rid, "app/AuthKey.p8")

        # nothing outside the run's folder
        with pytest.raises(ToolError, match="-> 400"):
            await workspace.view(rid, "app/peek.txt")
        with pytest.raises(ToolError, match="-> 404"):
            await workspace.view(rid, "../someone-else/secret.txt")  # stays inside: there's no such file here
        assert (await workspace.tree("no-such-run"))["exists"] is False
    loop.run_until_complete(go())


def test_files_routes(box, monkeypatch):
    from todd import main

    rid = new_run("files over http")
    _project(box, rid)
    monkeypatch.setattr(config, "api_token", "")
    c = TestClient(main.app)
    r = c.get(f"/api/runs/{rid}/files")
    assert r.status_code == 200 and any(e["path"] == "app/README.md" for e in r.json()["entries"])
    r = c.get(f"/api/runs/{rid}/files/view", params={"path": "app/README.md"})
    assert r.status_code == 200 and r.json()["content"] == "# Water tracker\n"
    assert c.get(f"/api/runs/{rid}/files/view", params={"path": "app/nope.md"}).status_code == 404
    r = c.get(f"/api/runs/{rid}/files/download", params={"path": "app/README.md"})
    assert r.status_code == 200 and r.content == b"# Water tracker\n"
    assert 'filename="README.md"' in r.headers["content-disposition"]
    img = c.get(f"/api/runs/{rid}/files/view", params={"path": "app/icon.png"}).json()
    assert img["mime"] == "image/png" and base64.b64decode(img["data"]).startswith(b"\x89PNG")
    assert c.get("/api/runs/nope/files").status_code == 404


def test_vault_keys_are_credited_to_the_run_that_saved_them(monkeypatch):
    from todd import main
    from todd.runtime import RunContext, _current_agent
    from todd.sdk import _current_ctx, set_ctx

    names = ("WS_MINE", "WS_RUN_KEY", "WS_SIGNIN")
    rid = new_run("Ship the water app")
    try:
        vault.set_secret("WS_MINE", "typed-in-settings")  # outside a run: the human
        vault.set_secret("WS_SIGNIN", "cli-state", source="sign-in")
        tok, atok = set_ctx(RunContext(rid)), _current_agent.set("a_keys")
        try:
            vault.set_secret("WS_RUN_KEY", "made-by-an-agent-123")
            vault.set_secret("WS_MINE", "refreshed-by-a-cli", track=False)  # keeps its credit
        finally:
            _current_ctx.reset(tok)
            _current_agent.reset(atok)
        rows = {r["name"]: r for r in vault.list_secrets() if r["name"] in names}
        assert rows["WS_MINE"]["source"] == "you" and rows["WS_MINE"]["run_id"] is None
        assert rows["WS_SIGNIN"]["source"] == "sign-in"
        key = rows["WS_RUN_KEY"]
        assert (key["run_id"], key["run_title"], key["agent"], key["source"]) == (rid, "Ship the water app", "a_keys",
                                                                                  "run")
        assert "made-by" not in str(rows)  # masked only
        assert [r["name"] for r in vault.list_secrets(rid)] == ["WS_RUN_KEY"]

        monkeypatch.setattr(config, "api_token", "")
        got = TestClient(main.app).get("/api/secrets", params={"run_id": rid}).json()
        assert [r["name"] for r in got] == ["WS_RUN_KEY"]
    finally:
        for n in names:
            vault.delete_secret(n)
    assert not [r for r in vault.list_secrets() if r["name"] in names]


def test_usage_totals_per_run_agent_and_day(monkeypatch):
    from todd import main, usage
    from todd.db import Event, session

    rid = new_run("usage")
    usage.record(rid, "planner", "claude-opus-5-5", **usage.from_anthropic(
        {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7}))
    usage.record(rid, "planner", "claude-opus-5-5", calls=0, plan_usd=0.25)
    usage.record(rid, "a_x", "gpt-5", cost_usd=0.5, **usage.from_langchain(
        {"input_tokens": 300, "output_tokens": 40, "input_token_details": {"cache_read": 200}}))
    usage.record(rid, "a_x", "gpt-5", calls=0)  # nothing to add
    with session() as s:
        s.add(Event(run_id=rid, agent="a_x", kind="tool_call", text="shell: ls", data={"tool": "shell"}))
        s.add(Event(run_id=rid, agent="a_x", kind="browser_step", text="Opened", data={}))
        s.commit()
    u = usage.run_usage(rid)
    t = u["total"]
    assert (t["calls"], t["input_tokens"], t["output_tokens"], t["cache_read_tokens"], t["cache_write_tokens"]) == \
        (2, 110, 45, 300, 7)
    assert t["cost_usd"] == 0.5 and t["plan_usd"] == 0.25 and t["tool_calls"] == 1 and t["browser_steps"] == 1
    x = next(a for a in u["agents"] if a["id"] == "a_x")
    assert x["top_tools"] == [["shell", 1]] or x["top_tools"] == [("shell", 1)]
    monkeypatch.setattr(config, "api_token", "")
    c = TestClient(main.app)
    assert c.get(f"/api/runs/{rid}/usage").json()["total"]["tokens"] == 462
    o = c.get("/api/usage", params={"days": 7}).json()
    assert len(o["series"]) == 7 and o["series"][-1]["tokens"] >= 462
    assert any(r["run_id"] == rid and r["title"] == "usage" for r in o["runs"])
