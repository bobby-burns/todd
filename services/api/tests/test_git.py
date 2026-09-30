"""Git the normal way: the `git` tool (GitHub sign-in only for commands that talk to a remote, hooks off then, no
config changes or program-running options) and git_push (commit, .gitignore, origin, push -u of the current branch).
Runs real git against a local bare repository."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from todd import vault
from todd.config import config
from todd.runtime import RunContext
from todd.sdk import ToolError, _current_ctx, set_ctx
from todd.tools import sandbox
from todd.tools.sandbox_tools import _git_argv, git, git_push

from .helpers import new_run

TOKEN = "ghp_gittest0123456789abcdefghijklmnopqrstu"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A local stand-in for the sandbox (real bash + git), a run folder and a bare 'GitHub' repo."""
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    calls: list[dict] = []

    async def exec_(cmd, cwd, timeout=300, env=None, signed_in=False):
        calls.append({"cmd": cmd, "env": dict(env or {}), "signed_in": signed_in})
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={"PATH": os.environ["PATH"], "HOME": str(home), **(env or {})})
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 5)
        return {"exit_code": proc.returncode, "output": out.decode()}

    monkeypatch.setattr(sandbox, "exec_", exec_)
    monkeypatch.setattr(config, "workspace_root", str(work))
    rid = new_run("git")
    (work / rid).mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    vault.set_secret("GITHUB_TOKEN", TOKEN)
    tok = set_ctx(RunContext(rid))
    yield {"dir": work / rid, "bare": bare, "calls": calls}
    _current_ctx.reset(tok)
    vault.delete_secret("GITHUB_TOKEN")


def _remote_log(bare, ref: str) -> str:
    return subprocess.run(["git", "--git-dir", str(bare), "log", "--format=%s", ref], capture_output=True,
                          text=True).stdout


def test_git_push_is_a_normal_push(loop, repo):
    d = repo["dir"]
    (d / "index.js").write_text("console.log('hi')\n")
    (d / "node_modules" / "x").mkdir(parents=True)
    (d / "node_modules" / "x" / "i.js").write_text("junk")
    (d / ".env").write_text("SECRET=1\n")

    async def go():
        r = await git.ainvoke({"command": f"init -q -b main"})
        assert r["exit_code"] == 0
        r = await git.ainvoke({"command": f"remote add origin {repo['bare']}"})
        assert r["exit_code"] == 0
        assert repo["calls"][-1]["env"] == {}  # local commands never get the token...
        assert repo["calls"][-1]["signed_in"] is False  # ...and run in the sandbox
        r = await git_push.ainvoke({"message": "First version"})
        assert r["exit_code"] == 0, r["output"]
        assert repo["calls"][-1]["env"] == {"GH_TOKEN": TOKEN} and TOKEN not in str(r)
        assert repo["calls"][-1]["signed_in"] is True  # the signed-in runner, when there is one
        assert "First version" in _remote_log(repo["bare"], "main")
        files = subprocess.run(["git", "--git-dir", str(repo["bare"]), "ls-tree", "-r", "--name-only", "main"],
                               capture_output=True, text=True).stdout.split()
        assert "index.js" in files and ".gitignore" in files
        assert not any(f.startswith("node_modules") or f == ".env" for f in files)  # the default .gitignore
        # a feature branch stays a feature branch (no renaming to main), with upstream tracking
        await git.ainvoke({"command": "checkout -q -b feature/x"})
        (d / "b.js").write_text("b\n")
        r = await git_push.ainvoke({"message": "Feature"})
        assert r["exit_code"] == 0, r["output"]
        assert "Feature" in _remote_log(repo["bare"], "feature/x") and "Feature" not in _remote_log(repo["bare"], "main")
        st = await git.ainvoke({"command": "status -sb"})
        assert "feature/x...origin/feature/x" in st["output"]
        r = await git.ainvoke({"command": "pull"})
        assert r["exit_code"] == 0, r["output"]

    loop.run_until_complete(go())


def test_git_refuses_config_tricks_and_program_options(loop, repo):
    for bad in ("-c core.sshCommand=evil push", "config user.name x", "fetch --upload-pack=evil origin",
                "clone -u evil https://github.com/a/b", "clone -c core.fsmonitor=evil https://github.com/a/b",
                "rebase -i HEAD~2", "push --receive-pack=evil origin main", "var -l", "credential fill",
                "-C /etc status"):
        with pytest.raises(ToolError):
            _git_argv(bad)
    assert _git_argv("git push -u origin main") == ["push", "-u", "origin", "main"]

    async def go():
        await git.ainvoke({"command": "init -q"})
        subprocess.run(["git", "-C", str(repo["dir"]), "config", "alias.push", "!env"], check=True)
        r = await git.ainvoke({"command": f"push {repo['bare']} HEAD"})
        assert r["exit_code"] != 0 and "refusing to run with a token" in r["output"] and TOKEN not in str(r)
        with pytest.raises(ToolError):
            await git_push.ainvoke({"repo": 'x/y" ; env ; echo "'})

    loop.run_until_complete(go())
