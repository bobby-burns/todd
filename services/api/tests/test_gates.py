"""What needs a person, enforced in code: purchases, card entry and public actions in the browser (a real Chromium
with x.com and a payment page mapped to a local server), known purchase and posting APIs and commands, the web tools'
connect-time address check, the shared browser lock, the live-view password and signed-in commands' runner."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from todd import gates
from todd.config import config
from todd.db import LedgerEntry, select, session
from todd.runtime import RunContext, resolve_interaction
from todd.sdk import ToolError, set_ctx, set_current_agent

from .helpers import new_run, pending


def test_purchase_labels_and_totals():
    for t in ["Buy now", "Upgrade to Pro", "Upgrade", "Subscribe", "Pay $12.00", "Place your order", "Start free trial",
              "Confirm and pay", "Add to cart", "Check out", "Purchase for $99", "Renew now", "Pay"]:
        assert gates.purchase_label(t), t
    for t in ["Upgrade guide", "Pricing", "Pay attention to this", "Payments", "Billing", "Deploy", "Continue",
              "Upgrade Next.js to 15", "PayPal"]:
        assert not gates.purchase_label(t), t
    assert gates.page_total("Subtotal $10.00\nTax $0.80\nTotal due today\n$10.80\nPay $10.80") == 10.8
    assert gates.page_total("Pro plan $20/month\nContinue") is None
    assert gates.page_total("Total: 1,234.50 USD") == 1234.5
    assert gates.allowed_total(10) == 11 and gates.allowed_total(100) == 103


def test_sites():
    assert gates.is_public_site("x.com") and gates.is_public_site("www.linkedin.com")
    assert gates.is_public_site("mail.google.com") and not gates.is_public_site("accounts.google.com")
    assert not gates.is_public_site("developer.x.com")  # getting API keys isn't posting
    assert gates.is_payment_host("checkout.stripe.com") and gates.is_payment_host("www.paypal.com")
    assert not gates.is_payment_host("stripe.com")
    assert gates.is_billing_page("https://vercel.com/acme/~/settings/billing") and \
        not gates.is_billing_page("https://vercel.com/docs")
    assert gates.check_card_domains(["checkout.stripe.com"])  # who gets paid?
    assert gates.check_card_domains(["vercel.com", "checkout.stripe.com"]) is None
    assert gates.exact("www.namecheap.com", "namecheap.com") and not gates.exact("ap.www.namecheap.com", "namecheap.com")


def test_card_details_only_on_the_exact_sites(monkeypatch):
    from todd import vault
    from todd.agents.browser import _card_secrets

    vault.set_secret("CARD_NUMBER", "4242424242424242")
    try:
        patterns = set(_card_secrets(["namecheap.com"]) or {})
        assert patterns == {"https://namecheap.com", "https://www.namecheap.com"}  # no *.namecheap.com
    finally:
        vault.delete_secret("CARD_NUMBER")


def test_known_purchase_and_posting_requests():
    cr = gates.check_request
    assert cr("POST", "https://api.vercel.com/v1/registrar/domains/example.com/buy")[0] == "purchase"
    assert cr("GET", "https://api.namecheap.com/xml.response?ApiUser=a&Command=namecheap.domains.create")[0] == \
        "purchase"
    assert cr("GET", "https://api.namecheap.com/xml.response?Command=namecheap.domains.getList") is None
    assert cr("POST", "https://api.x.com/2/tweets") == ("public", "posting on X")
    assert cr("POST", "https://api.resend.com/emails")[0] == "public"
    assert cr("POST", "https://api.github.com/repos/me/app/issues")[0] == "public"
    assert cr("PATCH", "https://api.github.com/repos/me/app", '{"private": false}')[0] == "public-repo"
    assert cr("PATCH", "https://api.github.com/repos/me/app", '{"description": "x"}') is None
    assert cr("POST", "https://api.stripe.com/v1/refunds")[0] == "live-stripe"
    assert cr("POST", "https://api.stripe.com/v1/products") is None
    assert cr("GET", "https://api.x.com/2/users/me") is None

    cc = gates.check_command
    assert cc("vercel", ["domains", "buy", "example.com"])[0] == "purchase"
    assert cc("vercel", ["deploy", "--prod", "--yes"])[0] == "live"  # see test_launch.py
    assert cc("stripe", ["refunds", "create", "--charge", "ch_1", "--live"])[0] == "public"
    assert cc("stripe", ["refunds", "create", "--charge", "ch_1"]) is None  # test mode
    assert cc("eas", ["submit", "-p", "ios"])[0] == "public"
    assert cc("github", ["repo", "create", "app", "--public", "--source", "."])[0] == "public-repo"
    assert cc("github", ["repo", "create", "app", "--private", "--source", "."]) is None
    assert cc("github", ["release", "create", "v1.0"])[0] == "public"
    assert cc("github", ["api", "repos/me/app/issues", "-f", "title=x"])[0] == "public"
    assert cc("github", ["api", "repos/me/app/issues"]) is None  # a GET
    assert cc("github", ["api", "-X", "PATCH", "repos/me/app", "-F", "private=false"])[0] == "public-repo"
    assert cc("github", ["api", "-X", "PATCH", "repos/me/app", "-f", "description=hi"]) is None
    assert cc("github", ["pr", "create", "--fill"]) is None
    assert gates.check_shell("cd pkg && npm publish --access public")
    assert gates.check_shell("pnpm -r publish") and gates.check_shell("docker push me/app:latest")
    assert gates.check_shell("npm run build && npm test") is None


def _auto(ctx: RunContext, decision: str = "approve") -> asyncio.Task:
    async def go():
        it = await pending(ctx.run_id)
        resolve_interaction(it.id, decision=decision, answer=None)
        return it

    return asyncio.create_task(go())


def test_api_and_cli_gates_wait_for_the_human(loop, monkeypatch):
    from todd import vault
    from todd.tools import sandbox, web
    from todd.tools.human import authorize_purchase
    from todd.tools.sandbox_tools import cli, gh, shell

    ran: list[str] = []

    async def exec_(cmd, cwd, timeout=300, env=None, signed_in=False):
        ran.append(cmd)
        return {"exit_code": 0, "output": "ok"}

    monkeypatch.setattr(sandbox, "exec_", exec_)
    sent: list[str] = []

    async def fake_send(c, method, url, *, follow, **kw):
        import httpx

        sent.append(f"{method} {url}")
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    monkeypatch.setattr(web, "_send", fake_send)
    monkeypatch.setattr(web, "check_url", lambda url: asyncio.sleep(0, result=url.split("/")[2]))

    async def go():
        ctx = RunContext(new_run("gates"))
        set_ctx(ctx)
        set_current_agent("a_ops")
        vault.set_secret("GITHUB_TOKEN", "ghp_" + "g" * 36)
        vault.set_secret("RESEND_API_KEY", "re_abcdefgh_" + "r" * 20)

        # a posting API waits for the human, who sees the exact request
        t = asyncio.create_task(web.api_request.ainvoke({
            "method": "POST", "url": "https://api.resend.com/emails",
            "headers": {"Authorization": "Bearer {{secret:RESEND_API_KEY}}"},
            "json_body": {"to": "someone@example.com", "subject": "Hi", "text": "hello"}}))
        it = await pending(ctx.run_id)
        assert "sending email" in it.prompt and "someone@example.com" in it.data["details"]
        assert "re_abcdefgh" not in it.data["details"]  # the placeholder, not the key
        assert not sent  # nothing went out yet
        resolve_interaction(it.id, decision="deny", answer=None)
        with pytest.raises(ToolError, match="didn't approve"):
            await t
        assert not sent

        # a public repo, a release: same
        waiter = _auto(ctx)
        await gh.ainvoke({"command": "release create v1.0 --generate-notes"})
        assert "release create" in (await waiter).data["details"] and any("release create" in c for c in ran)

        # npm publish from the shell
        waiter = _auto(ctx, "deny")
        with pytest.raises(ToolError):
            await shell.ainvoke({"cmd": "npm publish"})
        await waiter
        assert not any("npm publish" in c for c in ran)

        # buying needs an approved purchase first, which goes in the ledger
        from todd import connect

        monkeypatch.setattr(connect, "is_connected", lambda s: True)
        vault.set_secret(connect.state_name("vercel"), "x")
        with pytest.raises(ToolError, match="authorize_purchase"):
            await cli.ainvoke({"service": "vercel", "command": "domains buy example.com"})
        waiter = _auto(ctx)
        said = await authorize_purchase.ainvoke({"amount_usd": 20, "merchant": "Vercel", "description": "a domain",
                                                 "sites": ["vercel.com"]})
        await waiter
        assert "Approved" in said
        with session() as s:
            row = s.exec(select(LedgerEntry).where(LedgerEntry.run_id == ctx.run_id)).first()
        assert row and row.amount_usd == 20 and row.approved_by == "human"
        monkeypatch.setattr(connect, "run", lambda *a, **k: asyncio.sleep(0, result={"exit_code": 0, "output": "bought"}))
        assert (await cli.ainvoke({"service": "vercel", "command": "domains buy example.com"}))["output"] == "bought"
        set_current_agent("a_other")  # approvals belong to the agent that asked
        with pytest.raises(ToolError, match="authorize_purchase"):
            await cli.ainvoke({"service": "vercel", "command": "domains buy example.com"})
        for n in ("GITHUB_TOKEN", "RESEND_API_KEY", connect.state_name("vercel")):
            vault.delete_secret(n)

    loop.run_until_complete(go())


def test_web_tools_check_the_address_when_connecting(loop, monkeypatch):
    """DNS rebinding: a name that looked public for the check and points at 127.0.0.1 when connecting."""
    from todd.tools import web

    for v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(v, raising=False)
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: real("127.0.0.1", *a, **k)
                        if host == "rebind.example" else real(host, *a, **k))

    async def go():
        async with web.client(timeout=5) as c:
            with pytest.raises(ToolError, match="private address"):
                await c.get("http://rebind.example:9/")

    loop.run_until_complete(go())


def test_one_browser_lock_for_every_run(loop):
    async def go():
        a, b = RunContext(new_run("a")), RunContext(new_run("b"))
        assert a.browser_lock is b.browser_lock
        await a.browser_lock.acquire("run-a:agent")
        b.browser_lock.release("run-b:agent")  # not theirs: nothing happens
        assert b.browser_lock.locked() and b.browser_lock.holder == "run-a:agent"
        a.browser_lock.release("run-a:agent")
        assert not b.browser_lock.locked()

    loop.run_until_complete(go())


def test_live_view_url_has_its_password(tmp_path, monkeypatch):
    f = tmp_path / "vnc_password"
    f.write_text("Ab3dEf9h\n")
    monkeypatch.setattr(config, "browser_live_password", "")
    monkeypatch.setattr(config, "browser_live_password_file", str(f))
    monkeypatch.setattr(config, "browser_live_url", "http://localhost:6080/vnc_lite.html?scale=true")
    assert config.live_url() == "http://localhost:6080/vnc_lite.html?scale=true&password=Ab3dEf9h"


def test_signed_in_commands_go_to_the_runner(loop, monkeypatch):
    from todd.tools import sandbox

    seen: list[tuple[str, str]] = []

    class Client:
        def __init__(self, base_url, timeout):
            self.base = base_url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, path, json, headers):
            import httpx

            seen.append((self.base, headers["X-Sandbox-Token"]))
            return httpx.Response(200, json={"exit_code": 0, "output": ""}, request=httpx.Request("POST", path))

    monkeypatch.setattr(sandbox.httpx, "AsyncClient", Client)
    monkeypatch.setattr(config, "signedin_url", "http://signedin:7000")
    monkeypatch.setattr(config, "signedin_token", "runner-token")
    loop.run_until_complete(sandbox.exec_("git push", "/workspace", signed_in=True))
    loop.run_until_complete(sandbox.exec_("ls", "/workspace"))
    assert seen == [("http://signedin:7000", "runner-token"), (config.sandbox_url, config.sandbox_token)]


# ------------------------------------------------------------------------------------------ in a real browser
_CANDIDATES = ("/opt/pw-browsers/chromium-1194/chrome-linux/chrome", shutil.which("chromium") or "",
               shutil.which("chromium-browser") or "")
CHROME = os.getenv("CHROME_BIN") or next((p for p in _CANDIDATES if p and Path(p).exists()), "")

PAGES = {
    "/compose": "<textarea id=t></textarea><button id=post>Post</button>",
    "/pricing": "<h1>Pro</h1><p>$49/month</p><button>Upgrade to Pro</button> "
                "<a href='http://pay.example:{port}/c/pay/cs_live_abc'>Checkout</a>",
    "/c/pay/cs_live_abc": "<p>Total due today</p><p>$49.00</p><input id=card placeholder='Card number'>"
                          "<button>Pay $49.00</button>",
    "/docs": "<a href='/docs/2'>Upgrade guide</a><input id=q>",
}


@pytest.fixture
def chrome_with_hosts():
    if not CHROME:
        pytest.skip("no Chromium to test with")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        cdp_port = s.getsockname()[1]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(self.path.split("?")[0], "<p>ok</p>").replace("{port}", str(self.server.server_port))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body>{body}</body></html>".encode())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rules = "MAP x.com 127.0.0.1, MAP shop.example 127.0.0.1, MAP pay.example 127.0.0.1"
    prof = Path(f"/tmp/todd-gates-{cdp_port}")
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", f"--remote-debugging-port={cdp_port}",
                             f"--user-data-dir={prof}", f"--host-resolver-rules={rules}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    yield f"http://127.0.0.1:{cdp_port}", srv.server_port
    proc.terminate()
    srv.shutdown()
    shutil.rmtree(prof, ignore_errors=True)


def test_browser_gate_in_chromium(loop, chrome_with_hosts, monkeypatch):
    from browser_use import Browser, Tools

    cdp, port = chrome_with_hosts
    # a stand-in for checkout.stripe.com (whose real name the browser would force onto https)
    monkeypatch.setattr(gates, "PAYMENT_HOSTS", (*gates.PAYMENT_HOSTS, "pay.example"))

    async def go():
        ctx = RunContext(new_run("browser gates"))
        set_ctx(ctx)
        b = Browser(cdp_url=cdp, keep_alive=True)
        await b.start()
        try:
            async def act(tools, name, params, **kw):
                r = await tools.registry.execute_action(name, params, browser_session=b, **kw)
                return getattr(r, "error", None) or ""

            async def index_of(text):
                await b.get_browser_state_summary(include_screenshot=False)
                for i, n in (await b.get_selector_map()).items():
                    if text.lower() in n.get_meaningful_text_for_llm().lower() or \
                            text == (n.attributes or {}).get("id"):
                        return i
                raise AssertionError(f"no element {text!r}")

            tools = Tools()
            gates.install(tools, ctx, "a_web")
            # posting sites: reading is fine, acting waits for an approval of that site
            assert not await act(tools, "navigate", {"url": f"http://x.com:{port}/compose"})
            assert "request_approval" in await act(tools, "click", {"index": await index_of("Post")})
            assert "request_approval" in await act(tools, "input", {"index": await index_of("t"), "text": "hi"})
            gates.grant(ctx, "public", ["x.com"], "a_web")
            assert not await act(tools, "click", {"index": await index_of("Post")})
            # ordinary pages: "Upgrade guide" isn't a purchase, "Upgrade to Pro" is
            await act(tools, "navigate", {"url": f"http://shop.example:{port}/docs"})
            assert not await act(tools, "click", {"index": await index_of("Upgrade guide")})
            await act(tools, "navigate", {"url": f"http://shop.example:{port}/pricing"})
            assert "authorize_purchase" in await act(tools, "click", {"index": await index_of("Upgrade to Pro")})
            assert "Scripts can't click" in await act(tools, "evaluate",
                                                      {"code": "document.querySelector('button').click()"})
            gates.grant(ctx, "purchase", ["shop.example"], "a_web", amount_usd=49)
            assert not await act(tools, "click", {"index": await index_of("Upgrade to Pro")})

            # card entry: a shared payment page only when it came from the merchant, and only up to the amount
            card_tools = Tools()
            gates.install(card_tools, ctx, "a_pay", gates.Card(["shop.example", "pay.example"], 20))
            await act(card_tools, "navigate", {"url": f"http://pay.example:{port}/c/pay/cs_live_abc",
                                               "new_tab": True})
            err = await act(card_tools, "input", {"index": await index_of("card"),
                                                  "text": "<secret>card_number</secret>"})
            assert "wasn't opened from shop.example" in err, err
            await act(card_tools, "navigate", {"url": f"http://shop.example:{port}/pricing"})
            await act(card_tools, "click", {"index": await index_of("Checkout")})
            await asyncio.sleep(1)
            err = await act(card_tools, "input", {"index": await index_of("card"),
                                                  "text": "<secret>card_number</secret>"})
            assert "total of $49.00, more than the $20.00" in err, err
            card = {"index": await index_of("card"), "text": "<secret>card_number</secret>"}
            ok = gates.Card(["shop.example", "pay.example"], 49)
            assert await gates.check_browser(ctx, "a_pay2", "input", card, b, ok) is None
            other = gates.Card(["shop.example"], 49)  # the payment page itself wasn't approved
            assert "Card details go only" in await gates.check_browser(ctx, "a_pay3", "input", card, b, other)
        finally:
            await b.stop()

    loop.run_until_complete(go())
