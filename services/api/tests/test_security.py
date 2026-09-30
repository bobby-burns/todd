"""Secrets stay out of the model's context and the timeline: format-based redaction, scrubbing inside structured
tool results and events, secret answers from the human, the web tools' internal-address guard, the CLI blocklist
and saving command/API output straight into the vault."""

from __future__ import annotations

import asyncio

import pytest

from todd import redact, vault
from todd.sdk import ToolError, to_text

PEM = "-----BEGIN PRIVATE KEY-----\nMIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQgAbCdEf\n-----END PRIVATE KEY-----"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"


def test_known_formats_are_hidden_everywhere():
    text = (f"key sk_live_{'a1' * 12} and ghp_{'x' * 36}\n{PEM}\n"
            "https://user:hunter22@db.example.com/x and https://app.example.com/cb?code=4/0AbCdEfGhIjKlMnOpQrStUvWx&state=ok "
            "https://x.io/#access_token=ya29.a0AfH6SM&token_type=bearer")
    out = redact.known(text)
    assert "sk_live_" not in out and "ghp_" not in out and "PRIVATE KEY-----\nMIG" not in out
    assert "hunter22" not in out and "4/0AbCdEfGhIjKl" not in out and "ya29" not in out
    assert "state=ok" in out and "token_type=bearer" in out  # other parameters are left alone
    assert redact.known("{{secret:STRIPE_SECRET_KEY}} <secret>card_number</secret>") == \
        "{{secret:STRIPE_SECRET_KEY}} <secret>card_number</secret>"


def test_page_patterns_only_on_pages():
    sha, uuid = "3f786850e387550fdab836ed7e6dc881de23001b", "6f1c2b8e-5d4a-4c3e-9b1a-0d2e3f4a5b6c"
    random_key = "Xk2mN9pQ7rS4tV8wY1zA3bC6dE0fG5hJ"
    page = redact.page(f"anon {JWT} maps AIza{'B' * 35} key {random_key} commit {sha} id {uuid} hello_world_x")
    assert JWT not in page and "AIza" not in page and random_key not in page
    assert sha in page and uuid in page and "hello_world_x" in page
    assert redact.PAGE_MARK in page and redact.has_page_secret(JWT) and not redact.has_page_secret(sha)
    assert JWT in redact.known(JWT)  # command output keeps JWTs (public anon keys are normal in config)


def test_structured_results_are_scrubbed_before_json_hides_them():
    vault.set_secret("SEC_TEST_PEM", PEM)
    try:
        result = {"exit_code": 0, "output": f"cat key.p8\n{PEM}\n", "nested": [{"v": PEM}]}
        naive = vault.scrub(to_text(result), patterns=False)
        assert "MIGTAgEAMBMGByqGSM49" in naive or "\\n" in naive  # JSON escaping used to defeat the match
        clean = vault.scrub(to_text(redact.deep(result, vault.scrub)))
        assert "MIGTAgEAMBMGByqGSM49" not in clean
        import base64

        assert "***" in vault.scrub(base64.b64encode(PEM.encode()).decode())  # `base64 key.p8` too
    finally:
        vault.delete_secret("SEC_TEST_PEM")


def test_masked_values_show_little():
    vault.set_secret("SEC_TEST_SHORT", "abc12345")
    vault.set_secret("SEC_TEST_LONG", "sk_test_" + "q" * 30 + "WXYZ")
    try:
        rows = {r["name"]: r["masked"] for r in vault.list_secrets() if r["name"].startswith("SEC_TEST_")}
        assert rows == {"SEC_TEST_SHORT": "••••", "SEC_TEST_LONG": "••••WXYZ"}
    finally:
        vault.delete_secret("SEC_TEST_SHORT")
        vault.delete_secret("SEC_TEST_LONG")


def test_events_never_store_secrets():
    from todd import events
    from todd.db import Event, session

    from .helpers import new_run

    rid = new_run("events")
    vault.set_secret("SEC_TEST_EV", "value-known-to-the-vault-123")
    try:
        p = events.emit(rid, "a1", "thought", "I see value-known-to-the-vault-123 and sk_test_" + "z" * 20,
                        {"args": {"cmd": "echo ghp_" + "y" * 36}, "url": "https://x.com/?token=abcdefgh"})
        with session() as s:
            ev = s.get(Event, p["id"])
        blob = ev.text + str(ev.data)
        assert "value-known" not in blob and "sk_test_" not in blob and "ghp_" not in blob and "abcdefgh" not in blob
    finally:
        vault.delete_secret("SEC_TEST_EV")


def test_secret_answers_go_to_the_vault_and_pasted_ones_are_hidden(loop, monkeypatch):
    from fastapi.testclient import TestClient

    from todd import main
    from todd.config import config
    from todd.db import Interaction, select, session
    from todd.runtime import RunContext
    from todd.sdk import set_ctx
    from todd.tools.human import ask_human

    from .helpers import new_run

    monkeypatch.setattr(config, "api_token", "")
    c = TestClient(main.app)
    ctx = RunContext(new_run("asks"))
    tok = set_ctx(ctx)

    async def answer(text: str) -> None:
        for _ in range(200):
            with session() as s:
                it = s.exec(select(Interaction).where(Interaction.run_id == ctx.run_id,
                                                      Interaction.status == "pending")).first()
            if it:
                assert c.post(f"/api/interactions/{it.id}", json={"answer": text}).status_code == 200
                return
            await asyncio.sleep(0.02)

    async def go():
        t = asyncio.create_task(ask_human.ainvoke({"question": "DB password?", "secret_name": "SEC_TEST_DB_PW"}))
        await answer("correct horse battery staple")
        said = await t
        assert "Saved to the vault as SEC_TEST_DB_PW" in said and "horse" not in said
        assert vault.get_secret("SEC_TEST_DB_PW") == "correct horse battery staple"
        with session() as s:
            it = s.exec(select(Interaction).where(Interaction.run_id == ctx.run_id)).first()
        assert it.data["secret_name"] == "SEC_TEST_DB_PW" and "horse" not in (it.answer or "")
        credit = [r for r in vault.list_secrets(ctx.run_id) if r["name"] == "SEC_TEST_DB_PW"]
        assert credit and credit[0]["source"] == "run"

        t = asyncio.create_task(ask_human.ainvoke({"question": "Which key?"}))
        await answer(f"use sk_live_{'k' * 24} please")
        said = await t
        assert "sk_live_" not in said and "secret_name" in said
        with pytest.raises(ToolError):
            await ask_human.ainvoke({"question": "x", "secret_name": "GITHUB_TOKEN"})  # protected names can't be set

    try:
        loop.run_until_complete(go())
    finally:
        from todd.sdk import _current_ctx

        _current_ctx.reset(tok)
        vault.delete_secret("SEC_TEST_DB_PW")


def test_web_tools_stay_off_internal_addresses(loop):
    from todd.tools.web import check_url, fetch_url

    for url in ("http://localhost:8000/api", "http://sandbox:7000/exec", "http://browser:9223/json/list",
                "http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:5432", "http://metadata.google.internal/",
                "http://10.0.0.5/admin", "file:///etc/passwd"):
        with pytest.raises(ToolError):
            loop.run_until_complete(check_url(url))
    with pytest.raises(ToolError, match="internal"):
        loop.run_until_complete(fetch_url.ainvoke({"url": "http://api:8000/api/secrets"}))


def test_saving_output_and_responses_straight_to_the_vault():
    from todd.tools.sandbox_tools import _capture
    from todd.tools.web import _save_fields

    try:
        out, saved = _capture("Ready! Your webhook signing secret is whsec_abc123XYZ (^C to quit)",
                              {"SEC_TEST_WHSEC": r"(whsec_[A-Za-z0-9]+)"})
        assert saved == ["SEC_TEST_WHSEC"] and "whsec_" not in out and "{{secret:SEC_TEST_WHSEC}}" in out
        assert vault.get_secret("SEC_TEST_WHSEC") == "whsec_abc123XYZ"
        body = {"data": [{"id": "k1", "token": "tok_live_value_987"}]}
        assert _save_fields(body, {"SEC_TEST_TOKEN": "data.0.token"}) == ["SEC_TEST_TOKEN"]
        assert body["data"][0]["token"] == "{{secret:SEC_TEST_TOKEN}}"
        assert vault.get_secret("SEC_TEST_TOKEN") == "tok_live_value_987"
        with pytest.raises(ToolError):
            _save_fields({"a": 1}, {"SEC_TEST_X": "b"})
    finally:
        for n in ("SEC_TEST_WHSEC", "SEC_TEST_TOKEN"):
            vault.delete_secret(n)


def test_cli_blocklist_sees_past_global_flags():
    from todd.connect import _blocked

    assert _blocked(["--color", "off", "config", "--list"], "config")
    assert _blocked(["whoami", "--token"], "whoami --token")
    assert not _blocked(["whoami"], "whoami --token")
    assert _blocked(["env:list", "--plain"], "env:list")
    assert not _blocked(["deploy", "--prod", "--yes"], "login")


def test_hidden_values_are_never_written_back(loop):
    from todd.tools.sandbox_tools import write_file

    assert redact.hidden_markers("API_KEY=••••••••") and redact.hidden_markers(f"x {redact.MARK}")
    assert not redact.hidden_markers("normal *** markdown")
    with pytest.raises(ToolError, match="hid from you"):
        loop.run_until_complete(write_file.ainvoke({"path": ".env", "content": "API_KEY=••••••••\nNEW=1\n"}))
    # common parameter names only hide long values
    assert redact.urls("https://x.com/?key=abc&code=print") == "https://x.com/?key=abc&code=print"
    assert "AIzaSy" not in redact.urls("https://maps.googleapis.com/api?key=AIzaSyB1234567890abcdefgh")
