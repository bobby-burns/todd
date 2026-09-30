"""What needs a person, enforced in code rather than by asking the model nicely.

A prompt-injected page can tell an agent anything; these checks don't read what the model says, only what it does.

Browser (every click, keystroke and script, both engines):
* Money. Clicking something labelled like a purchase (Buy, Upgrade to Pro, Subscribe, Pay $12, Place order, Start
  trial…), or doing anything on a payment page (Stripe Checkout, PayPal, Paddle…) or a billing page (…/billing,
  …/checkout, …/upgrade), needs an approved purchase for that site: `authorize_purchase` (the human approves; it goes
  in the Ledger) or a card checkout started with payment_*. Before the click or the card is typed, the page's total
  must fit the approved amount.
* Card details. Typed only on the exact approved hosts (no subdomains); on a payment page shared by every merchant
  (checkout.stripe.com, paypal.com…) only when the tab got there from the approved merchant's own site.
* Public actions. Doing anything on a site where the human's account posts, messages or emails people (x.com,
  LinkedIn, Reddit, Gmail…) needs their OK for that site first (`request_approval(..., sites=[...])`), for a while.
  Reading and scrolling never need it.

APIs and CLIs (`api_request`, `cli`, `gh`, `eas`, `shell`): known purchase and publishing endpoints and commands open
an approval showing the exact request before they run (see `check_request` and `check_command`).

These are backstops, not a proof: a purchase with no recognizable label or page, or an unknown posting API, gets
through. The real limits on money are the run budget, the auto-approve limit, and a card with its own limit.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

GRANT_MINUTES = 30

# ------------------------------------------------------------------------------------------ sites
# Payment pages shared by many merchants: approving one doesn't say who gets paid.
PAYMENT_HOSTS = (
    "checkout.stripe.com", "buy.stripe.com", "pay.stripe.com", "invoice.stripe.com", "billing.stripe.com", "link.com",
    "paypal.com", "checkout.shopify.com", "pay.shopify.com", "shop.app", "checkout.paddle.com", "buy.paddle.com",
    "pay.paddle.io", "lemonsqueezy.com", "gumroad.com", "pay.google.com", "payments.google.com", "square.link",
    "checkout.square.site", "chargebee.com", "recurly.com", "fastspring.com", "2checkout.com", "braintreegateway.com",
    "checkoutshopper-live.adyen.com", "checkout.razorpay.com", "polar.sh",
)
# Where the human's account speaks to other people.
PUBLIC_SITES = (
    "x.com", "twitter.com", "linkedin.com", "facebook.com", "instagram.com", "threads.net", "threads.com", "bsky.app",
    "reddit.com", "news.ycombinator.com", "producthunt.com", "tiktok.com", "youtube.com", "medium.com",
    "substack.com", "discord.com", "slack.com", "mastodon.social", "pinterest.com", "quora.com", "dev.to",
    "hashnode.com", "indiehackers.com", "web.whatsapp.com", "messenger.com", "web.telegram.org",
    "mail.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com", "mail.yahoo.com",
    "mail.proton.me", "app.hey.com",
)
# Hosting dashboards, where a Deploy / Publish / Promote click makes a site live.
HOSTING_SITES = ("vercel.com", "netlify.com", "app.netlify.com", "dash.cloudflare.com", "console.firebase.google.com",
                 "railway.com", "railway.app", "render.com", "fly.io", "expo.dev")
_LIVE_LABEL = re.compile(r"^(re)?deploy\b|^publish\b|^go live\b|^promote\b|^make (it )?live\b|^launch\b|"
                         r"^(add|connect|assign) (a )?(custom )?domain\b", re.I)
_PUBLIC_REPO_LABEL = re.compile(r"\bmake (this repository |it )?public\b|\bchange to public\b", re.I)
BILLING_SEGMENTS = {"billing", "checkout", "upgrade", "subscribe", "subscription", "subscriptions", "payment",
                    "payments", "purchase", "cart"}
LOGIN_SEGMENTS = {"login", "signin", "sign-in", "log-in", "sso", "oauth", "authorize", "2fa", "verify"}
# What changes something (reading, scrolling, navigating and waiting don't).
ACTING = {"click", "input", "send_keys", "select_dropdown", "upload_file", "evaluate"}
_SCRIPT_ACTS = re.compile(r"\.(?:click|submit|requestSubmit|dispatchEvent)\s*\(|\bfetch\s*\(|XMLHttpRequest|sendBeacon",
                          re.I)


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def under(host: str, site: str) -> bool:
    """host is the site or one of its subdomains ("*" is any site)."""
    if site == "*":
        return True
    site = site.lower().removeprefix("www.")
    return host == site or host.endswith("." + site)


def exact(host: str, site: str) -> bool:
    """host is exactly the site (www. counts as the same site)."""
    return host.removeprefix("www.") == site.lower().removeprefix("www.")


def is_local(host: str) -> bool:
    """The agents' own previews (http://localhost:3000 …) are theirs to click around in."""
    return host in ("localhost", "127.0.0.1", "[::1]", "::1") or host.endswith(".localhost")


def is_payment_host(host: str) -> bool:
    return any(under(host, p) for p in PAYMENT_HOSTS)


def is_public_site(host: str) -> bool:
    if host.split(".")[0].startswith("developer"):  # developer portals (API keys, app settings) aren't posting
        return False
    return any(under(host, p) for p in PUBLIC_SITES)


def _segments(url: str) -> set[str]:
    try:
        return {s.lower() for s in urlparse(url).path.split("/") if s}
    except ValueError:
        return set()


def is_billing_page(url: str) -> bool:
    return bool(_segments(url) & BILLING_SEGMENTS)


def is_test_checkout(url: str) -> bool:
    """Stripe test mode (an app being built): no real money."""
    return "cs_test_" in url or "/test_" in url


_PRICE = r"(?:[$€£]\s?\d|\d[\d.,]*\s?(?:usd|eur|gbp|\$|€|£))"


def purchase_label(text: str) -> bool:
    """Whether a button or link label reads like a purchase: "Buy now", "Upgrade to Pro", "Pay $12.00", "Place
    order", "Start free trial", "Confirm and pay"… (but not "Upgrade guide" or "Pricing")."""
    t = re.sub(r"[\s ]+", " ", (text or "")).strip().lower()
    t = re.sub(r"[\s→›»>.!]+$", "", t)
    if not t or len(t) > 60:
        return False
    if re.match(r"(buy|purchase)\b", t):
        return len(t.split()) <= 6
    phrases = (r"place (your )?order", r"complete (your )?(order|purchase|payment|checkout|upgrade)",
               r"confirm (and|&) pay", r"confirm (purchase|payment|order|subscription|upgrade|plan)",
               r"submit (order|payment)", r"start (my |your )?(free )?(trial|subscription|plan)", r"add to cart",
               r"(get|go) (pro|premium|plus)", r"top up", r"(add|buy) (credits|funds|seats)", r"register (domain|now)",
               r"check ?out", r"pay (now|with)\b")
    if any(re.match(p + r"\b", t) for p in phrases):
        return True
    m = re.match(r"(upgrade|subscribe|renew|pay|enroll)\b ?(.*)$", t)
    if not m:
        return False
    rest = m.group(2)
    return (rest in ("", "now", "plan", "account", "subscription", "today") or rest.startswith(("to ", "for "))
            or bool(re.search(_PRICE, rest)))


# ------------------------------------------------------------------------------------------ grants
@dataclass
class Grant:
    kind: str  # "purchase", "public" or "live"
    sites: tuple[str, ...]
    agent: str
    until: float
    amount_usd: float | None = None
    ledger_id: int | None = None
    note: str = ""


def _grants(ctx: Any) -> list[Grant]:
    if not hasattr(ctx, "_grants"):
        ctx._grants = []
    return ctx._grants


def grant(ctx: Any, kind: str, sites: list[str], agent: str, *, amount_usd: float | None = None,
          ledger_id: int | None = None, minutes: int = GRANT_MINUTES, note: str = "") -> Grant:
    g = Grant(kind, tuple(s.lower().removeprefix("www.") for s in sites), agent, time.time() + minutes * 60,
              amount_usd, ledger_id, note)
    _grants(ctx).append(g)
    return g


def granted(ctx: Any, kind: str, host: str, agent: str) -> Grant | None:
    now = time.time()
    for g in reversed(_grants(ctx)):
        if g.kind == kind and g.agent in (agent, "*") and g.until > now and any(under(host, s) for s in g.sites):
            return g
    return None


# ------------------------------------------------------------------------------------------ amounts
_MONEY = re.compile(r"(?:US\$|USD|[$€£])\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
                    r"|(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?(?:USD|US\$|[$€£])", re.I)
_TOTAL_WORDS = re.compile(r"\b(total|due|pay|payment|charged?|amount|today)\b", re.I)


def page_total(text: str) -> float | None:
    """The largest amount the page shows next to a total-like word ("Total due today $12.00", "Pay $12.00"), or
    None when it shows none."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    best: float | None = None
    for i, line in enumerate(lines[:4000]):
        if not _TOTAL_WORDS.search(line):
            continue
        found = list(_MONEY.finditer(line)) or (list(_MONEY.finditer(lines[i + 1])) if i + 1 < len(lines) else [])
        for m in found:
            v = float((m.group(1) or m.group(2)).replace(",", ""))
            best = v if best is None else max(best, v)
    return best


def allowed_total(approved: float) -> float:
    """A little room for rounding and tax."""
    return round(approved + max(1.0, approved * 0.03), 2)


# ------------------------------------------------------------------------------------------ the browser gate
@dataclass
class Card:
    """An approved card checkout: where the card may be typed, and for how much."""
    domains: list[str]
    amount_usd: float
    merchants: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.merchants = [d for d in self.domains if not is_payment_host(d)]


def check_card_domains(domains: list[str]) -> str | None:
    """A problem with the payment_domains an agent asked for, or None."""
    if any(is_payment_host(d) for d in domains) and not any(not is_payment_host(d) for d in domains):
        shared = ", ".join(d for d in domains if is_payment_host(d))
        return (f"{shared} is a payment page every merchant uses, so it doesn't say who gets paid. Also list the "
                "merchant's own site in payment_domains (e.g. ['vercel.com', 'checkout.stripe.com']): the card is "
                "only filled in there when the checkout was opened from that site.")
    return None


_ACTIVE_LABELS_JS = r"""(() => { const el = document.activeElement; const txt = (e) => e ? ((e.innerText || e.value ||
  e.getAttribute('aria-label') || '') + '').slice(0, 120) : '';
  const sub = el && el.form ? el.form.querySelector('[type=submit],button:not([type])') : null;
  return [txt(el), txt(sub)]; })()"""


async def _eval(session: Any, expression: str) -> Any:
    s = await session.get_or_create_cdp_session(focus=False)
    r = await s.cdp_client.send.Runtime.evaluate(params={"expression": expression, "returnByValue": True},
                                                 session_id=s.session_id)
    return (r.get("result") or {}).get("value")


async def _page_text(session: Any) -> str:
    try:
        return str(await _eval(session, "document.body ? document.body.innerText.slice(0, 300000) : ''") or "")
    except Exception:  # noqa: BLE001
        return ""


async def _label(session: Any, action: str, params: dict) -> str:
    try:
        if action == "click":
            node = await session.get_element_by_index(int(params.get("index")))
            if node is None:
                return ""
            attrs = getattr(node, "attributes", None) or {}
            return " ".join(filter(None, [node.get_meaningful_text_for_llm(), attrs.get("aria-label", "")]))[:200]
        if action == "send_keys" and re.search(r"enter|space|return", str(params.get("keys", "")), re.I):
            active, submit = await _eval(session, _ACTIVE_LABELS_JS) or ["", ""]
            return active if purchase_label(active) else submit
    except Exception:  # noqa: BLE001
        return ""
    return ""


async def _came_from(session: Any) -> list[str]:
    """Hosts this tab visited before the current page (newest first), then the tab that opened it."""
    hosts: list[str] = []
    try:
        s = await session.get_or_create_cdp_session(focus=False)
        h = await s.cdp_client.send.Page.getNavigationHistory(session_id=s.session_id)
        entries = h.get("entries") or []
        idx = int(h.get("currentIndex", len(entries) - 1))
        hosts = [host_of(e.get("url", "")) for e in reversed(entries[:idx])]
        tid = getattr(session, "agent_focus_target_id", None)
        if tid:
            info = (await session.cdp_client.send.Target.getTargetInfo(params={"targetId": tid})).get("targetInfo", {})
            opener = info.get("openerId")
            if opener:
                o = (await session.cdp_client.send.Target.getTargetInfo(params={"targetId": opener})).get("targetInfo")
                hosts.append(host_of((o or {}).get("url", "")))
    except Exception:  # noqa: BLE001
        pass
    return [h for h in hosts if h]


def _over(total: float | None, approved: float) -> str | None:
    if total is not None and total > allowed_total(approved):
        return (f"The page shows a total of ${total:,.2f}, more than the ${approved:,.2f} that was approved. Don't "
                "continue: tell the human, or get the new amount approved first.")
    return None


async def check_browser(ctx: Any, agent: str, action: str, params: dict, session: Any,
                        card: Card | None = None) -> str | None:
    """Why this browser action must not run yet (for the agent), or None."""
    if action == "navigate" and str(params.get("url", "")).strip().lower().startswith("javascript:"):
        return "Open pages by their http(s) address; javascript: links can't be navigated to here."
    if action not in ACTING:
        return None
    url = await session.get_current_page_url()
    host = host_of(url)
    if not host or is_local(host) or not url.startswith(("http://", "https://")):
        return None
    if action == "evaluate" and _SCRIPT_ACTS.search(str(params.get("code", ""))):
        return ("Scripts can't click, submit or send requests on websites here: use the click and input actions, so "
                "Todd can check what's being done.")

    # Posting, messaging or emailing from the human's accounts
    if is_public_site(host) and not (_segments(url) & LOGIN_SEGMENTS) and not granted(ctx, "public", host, agent):
        return (f"{host} speaks for the human (posts, messages, email), so doing anything there needs their OK first. "
                f"Call request_approval with the exact text and where it goes, and sites=[\"{host}\"]. Reading and "
                "scrolling are fine without it.")

    # Card details: exact site, right merchant, right amount
    typing = str(params.get("text", ""))
    if action == "input" and "<secret>card_" in typing:
        if card is None:
            return "No card checkout was approved. Start the browser with the payment_* arguments first."
        if not any(exact(host, d) for d in card.domains):
            return (f"Card details go only to the approved sites ({', '.join(card.domains)}), not {host}. If this is "
                    "the right checkout, finish (browser_done) and start again with this exact site in payment_domains.")
        if is_payment_host(host):
            earlier = await _came_from(session)
            prev = next((h for h in earlier if not is_payment_host(h)), None)
            if not prev or not any(under(prev, m) for m in card.merchants):
                return (f"{host} is a payment page any merchant can use, and this one wasn't opened from "
                        f"{' or '.join(card.merchants) or 'the approved merchant'} (it came from {prev or 'nowhere'}). "
                        "Open the checkout from the merchant's own site, in this tab.")
        return _over(_total_or_none(await _page_text(session)), card.amount_usd)

    label = await _label(session, action, params)
    # Going live, and making a repository public (the human's launch plan decides; otherwise they're asked)
    if action == "click" and label:
        from . import launch

        plan = launch.effective(ctx)
        if any(under(host, h) for h in HOSTING_SITES) and _LIVE_LABEL.search(label.strip()) and \
                plan["live"] != "auto" and not granted(ctx, "live", "*", agent):
            return (f"\"{label.strip()[:40]}\" would put the site live, which waits for the human. Call "
                    "go_live(what, where) first (they're asked once for this run), then click again.")
        if under(host, "github.com") and _PUBLIC_REPO_LABEL.search(label) and plan["repo"] != "public" and \
                not granted(ctx, "public", host, agent):
            return ("Making the repository public needs the human's OK (their launch plan says private): call "
                    "request_approval(..., sites=[\"github.com\"]) first.")

    # Money
    why = ("a payment page" if is_payment_host(host) and not is_test_checkout(url) else
           "a billing page" if is_billing_page(url) else
           f"\"{label.strip()[:40]}\" looks like a purchase" if purchase_label(label) else None)
    if why is None:
        return None
    if card is not None and any(under(host, d) for d in card.domains):
        return _over(_total_or_none(await _page_text(session)), card.amount_usd) if purchase_label(label) else None
    g = granted(ctx, "purchase", host, agent)
    if g is None:
        return (f"This may cost money ({why} on {host}). Call authorize_purchase(amount_usd, merchant, description, "
                f"sites=[\"{host}\"]) first: the human approves it. If you're sure it's free, use amount_usd=0.")
    if g.amount_usd and purchase_label(label):
        return _over(_total_or_none(await _page_text(session)), g.amount_usd)
    return None


def _total_or_none(text: str) -> float | None:
    return page_total(text) if text else None


def install(tools: Any, ctx: Any, agent: str, card: Card | None = None) -> None:
    """Put the gate in front of every action a browser-use Tools runs (the `browse` agent's and the direct tools')."""
    from browser_use.agent.views import ActionResult

    registry = tools.registry
    if getattr(registry, "_todd_gated", False):
        return
    inner = registry.execute_action

    async def gated(action_name: str, params: dict, browser_session: Any = None, **kw: Any) -> Any:
        if browser_session is not None:
            problem = await check_browser(ctx, agent, action_name, params or {}, browser_session, card)
            if problem:
                ctx.emit(agent, "status", f"Held back: {problem[:240]}", {"gate": action_name})
                return ActionResult(error=problem)
        return await inner(action_name, params, browser_session=browser_session, **kw)

    registry.execute_action = gated
    registry._todd_gated = True


def clean_sites(sites: list[str] | None) -> list[str]:
    from .tools.browser_tools import clean_domain

    out = [clean_domain(s) for s in (sites or [])]
    if any(d is None for d in out):
        from .sdk import ToolError

        raise ToolError("sites must be site names like ['x.com'] or ['vercel.com'] (no wildcards).")
    return [d for d in out if d]


async def approve_purchase(ctx: Any, agent: str, amount_usd: float, merchant: str, description: str,
                           sites: list[str]) -> str:
    """The human approves a purchase on `sites` (always a person: it's a click on a site that may have a saved card);
    then the agent may do it there for a while. amount_usd=0: the agent says it's free; the human confirms that."""
    from .policy import authorize_spend
    from .sdk import ToolError

    sites = clean_sites(sites)
    if not sites:
        raise ToolError("Say where: sites=['vercel.com'] (the site where you'll click Buy/Upgrade/Pay).")
    amount = round(float(amount_usd or 0), 2)
    where = ", ".join(sites)
    if amount <= 0:
        ok, note = await ctx.request_approval(
            f"{merchant}: {description} (the agent says this is free) on {where}", agent=agent,
            data={"details": f"The agent wants to click through something on {where} that looks like a purchase, and "
                             "says it costs nothing. Approve only if that's right.", "sites": sites})
        if not ok:
            raise ToolError(f"The human said no. {note}".strip())
        grant(ctx, "purchase", sites, agent, amount_usd=0.0, note=description)
        return f"Approved as free on {where} for the next {GRANT_MINUTES} minutes. If a price appears, stop and ask."
    entry = await authorize_spend(ctx, amount_usd=amount, merchant=merchant, method="saved payment method",
                                  description=f"{description} (on {where})", agent=agent,
                                  data={"sites": sites}, require_human=True)
    grant(ctx, "purchase", sites, agent, amount_usd=amount, ledger_id=entry.id, note=description)
    return (f"Approved: up to ${amount:,.2f} at {merchant} on {where}, for the next {GRANT_MINUTES} minutes. Do only "
            "this purchase; if the total is higher, stop and ask.")


async def approve_public(ctx: Any, agent: str, action: str, details: str, sites: list[str] | None) -> dict:
    """request_approval: the human decides; with sites, an approval also opens those sites to this agent for a while."""
    sites = clean_sites(sites)
    approved, note = await ctx.request_approval(action, agent=agent,
                                                data={"details": details, **({"sites": sites} if sites else {})})
    out: dict = {"approved": approved, "note": note}
    if approved and sites:
        grant(ctx, "public", sites, agent, note=action)
        out["sites"] = f"You may act on {', '.join(sites)} for the next {GRANT_MINUTES} minutes, for this only."
    return out


# ------------------------------------------------------------------------------------------ APIs and CLIs
# (host pattern, methods, path pattern, what it does). Host patterns match the host or its subdomains; "*" is any host.
_W = r"[^/]+"
_NAMECHEAP_BUY = r"namecheap\.(domains\.(create|renew|reactivate|transfer\.create)|ssl\.create)"
PURCHASE_ENDPOINTS: list[tuple[str, str, str, str]] = [
    ("api.vercel.com", "POST", rf"/v\d+/(registrar/)?domains/({_W}/)?(buy|renew|transfer)", "buying a domain"),
    ("namecheap.com", "GET POST", _NAMECHEAP_BUY, "buying or renewing a domain"),  # the command is a parameter
    ("api.godaddy.com", "POST", r"/v\d+/(customers/[^/]+/)?domains/(purchase|register|[^/]+/renew|[^/]+/transfer)",
     "buying or renewing a domain"),
    ("porkbun.com", "POST", r"/api/json/v3/domain/(create|renew)", "buying or renewing a domain"),
    ("api.twilio.com", "POST", r"/IncomingPhoneNumbers(\.json)?$", "buying a phone number"),
    ("api.digitalocean.com", "POST", r"/v2/(droplets|databases|kubernetes/clusters|volumes|apps|load_balancers)$",
     "creating a paid server or database"),
    ("api.hetzner.cloud", "POST", r"/v1/(servers|volumes|load_balancers)$", "creating a paid server"),
    ("api.linode.com", "POST", r"/v4/(linode/instances|databases/[^/]+/instances)$", "creating a paid server"),
    ("api.vultr.com", "POST", r"/v2/(instances|bare-metals|databases)$", "creating a paid server"),
]
PUBLIC_ENDPOINTS: list[tuple[str, str, str, str]] = [
    ("api.twitter.com", "POST", r"/(2/tweets|1\.1/statuses/update|2/dm_conversations)", "posting on X"),
    ("api.x.com", "POST", r"/(2/tweets|1\.1/statuses/update|2/dm_conversations)", "posting on X"),
    ("api.linkedin.com", "POST", r"/(v2/ugcPosts|rest/posts|v2/shares|rest/socialActions)", "posting on LinkedIn"),
    ("graph.facebook.com", "POST", r"/(feed|media_publish|photos|videos|messages|comments)$",
     "posting or messaging on Facebook/Instagram/WhatsApp"),
    ("graph.instagram.com", "POST", r"/(media_publish|messages|comments)$", "posting on Instagram"),
    ("oauth.reddit.com", "POST", r"/api/(submit|comment|compose)", "posting on Reddit"),
    ("*", "POST", r"/xrpc/com\.atproto\.repo\.createRecord", "posting on Bluesky"),
    ("*", "POST", r"/api/v1/statuses$", "posting on Mastodon"),
    ("discord.com", "POST", r"/api/(v\d+/)?(webhooks|channels/[^/]+/messages)", "posting on Discord"),
    ("slack.com", "POST", r"/api/chat\.(postMessage|scheduleMessage)", "posting on Slack"),
    ("hooks.slack.com", "POST", r"/services/", "posting on Slack"),
    ("api.telegram.org", "POST GET", r"/bot[^/]+/send", "sending a Telegram message"),
    ("gmail.googleapis.com", "POST", r"/(messages|drafts)/send$", "sending email"),
    ("graph.microsoft.com", "POST", r"/(sendMail|messages/[^/]+/send)$", "sending email"),
    ("api.sendgrid.com", "POST", r"/v3/mail/send", "sending email"),
    ("api.resend.com", "POST", r"/emails", "sending email"),
    ("api.postmarkapp.com", "POST", r"/email", "sending email"),
    ("api.mailgun.net", "POST", r"/messages$", "sending email"),
    ("api.brevo.com", "POST", r"/v3/smtp/email", "sending email"),
    ("api.twilio.com", "POST", r"/Messages(\.json)?$", "sending a text message"),
    ("api.github.com", "POST", r"/repos/[^/]+/[^/]+/(issues(/\d+/comments)?|pulls/\d+/(comments|reviews)|releases)$",
     "posting on GitHub (an issue, comment, review or release)"),
    ("api.github.com", "POST", r"/(gists)$", "publishing a gist"),
]
_PUBLIC_REPO = re.compile(r"\bprivate\"?\s*[:=]\s*false\b|\bvisibility\"?\s*[:=]\s*\"?public\b", re.I)


def _match(table: list[tuple[str, str, str, str]], method: str, host: str, target: str) -> str | None:
    for pat, methods, path, label in table:
        if (pat == "*" or under(host, pat)) and method in methods.split() and re.search(path, target, re.I):
            return label
    return None


def check_request(method: str, url: str, body: str = "") -> tuple[str, str] | None:
    """(kind, what it does) when an API call buys something, speaks for the human, makes a repository public or
    puts something live (kinds as for check_command, plus "live-stripe")."""
    method = method.upper()
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host, target = (u.hostname or "").lower(), u.path + ("?" + u.query if u.query else "")
    label = _match(PURCHASE_ENDPOINTS, method, host, target) or (
        "buying or renewing a domain" if under(host, "namecheap.com") and re.search(_NAMECHEAP_BUY, body or "", re.I)
        else None)
    if label:
        return "purchase", label
    label = _match(PUBLIC_ENDPOINTS, method, host, u.path)
    if label:
        return "public", label
    repo_path = re.search(r"^/(repos/[^/]+/[^/]+|user/repos|orgs/[^/]+/repos)$", u.path)
    if under(host, "api.github.com") and method in ("PATCH", "POST") and repo_path and _PUBLIC_REPO.search(body or ""):
        return "public-repo", "making a GitHub repository public"
    if under(host, "api.vercel.com") and method == "POST" and re.search(r"^/v\d+/deployments$", u.path) and \
            re.search(r"\"target\"\s*:\s*\"production\"", body or ""):
        return "live", "a production deploy on Vercel (the live site)"
    if under(host, "api.vercel.com") and method == "POST" and re.search(r"^/v\d+/deployments$", u.path):
        return "deploy", "a deploy on Vercel"
    if under(host, "api.vercel.com") and method == "POST" and re.search(r"^/v\d+/projects/[^/]+/domains$", u.path):
        return "live", "pointing a domain at the site on Vercel"
    if under(host, "api.stripe.com") and method in ("POST", "DELETE") and re.search(
            r"^/v1/(charges|payment_intents|payouts|transfers|refunds|topups|invoices/[^/]+/(pay|send)|subscriptions|"
            r"credit_notes|application_fees/[^/]+/refunds)", u.path):
        return "live-stripe", "moving real money in Stripe (charges, refunds, payouts, subscriptions)"
    return None


_SHELL_PUBLISH = re.compile(
    r"\b(?:(?:npm|pnpm|yarn|bun)\s+(?:[\w-]+\s+)*publish|npx\s+(?:-y\s+)?(?:np|release-it|semantic-release)|"
    r"twine\s+upload|cargo\s+publish|gem\s+push|docker\s+push|vsce\s+publish|ovsx\s+publish|flutter\s+pub\s+publish|"
    r"eas\s+submit|fastlane\s+\w*\s*(?:deliver|pilot|upload_to_app_store|supply))\b", re.I)


def check_shell(cmd: str) -> str | None:
    """What a shell command publishes to the world (npm publish, docker push…), if anything."""
    m = _SHELL_PUBLISH.search(cmd or "")
    return f"publishing with `{m.group(0).strip()}`" if m else None


def _positionals(argv: list[str]) -> list[str]:
    return [a for a in argv if not a.startswith("-")]


def check_command(service: str, argv: list[str]) -> tuple[str, str] | None:
    """(kind, what it does) for a signed-in CLI command that buys something, publishes, makes a repository public or
    deploys: kind is "purchase", "public", "public-repo", "live" (production) or "deploy" (a preview; see hold)."""
    words = _positionals(argv)
    flags = {a.split("=", 1)[0] for a in argv if a.startswith("-")}
    first = words[0] if words else ""
    if service == "vercel" and first == "domains" and len(words) > 1 and words[1] in ("buy", "transfer-in", "renew"):
        return "purchase", "buying a domain (use vercel_buy_domain, which checks the price)"
    if service == "stripe" and "--live" in flags and set(words) & {
            "create", "update", "delete", "confirm", "capture", "pay", "cancel", "post", "void", "finalize", "send",
            "refund", "reverse"}:
        return "public", "a live-mode Stripe change (real customers and real money)"
    if service in ("eas", "expo") and first == "submit":
        return "public", "sending the app to Apple / Google (App Store Connect, Google Play)"
    deploy = _deploy_command(service, words, flags, argv)
    if deploy:
        return deploy
    if service == "github":
        sub = words[1] if len(words) > 1 else ""
        if first == "release" and sub == "create":
            return "public", "publishing a GitHub release"
        if first == "repo" and sub == "create" and "--public" in flags:
            return "public-repo", "creating a public GitHub repository"
        if first == "repo" and sub == "edit" and "--visibility" in flags and "public" in argv:
            return "public-repo", "making a GitHub repository public"
        if first == "gist" and sub == "create" and flags & {"--public", "-p"}:
            return "public", "publishing a public gist"
        if (first, sub) in (("issue", "create"), ("issue", "comment"), ("pr", "comment"), ("pr", "review")):
            return "public", f"posting on GitHub ({first} {sub})"
        if first == "api":
            takes_value = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header", "--input", "-q",
                           "--jq", "-t", "--template", "-p", "--preview", "--cache"}
            rest = argv[argv.index("api") + 1:]
            path = next((a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or rest[i - 1] not in
                                                                                    takes_value)), "")
            fields = flags & {"-f", "-F", "--field", "--raw-field", "--input"}
            method = (next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-X", "--method")), None)
                      or next((a.split("=", 1)[1] for a in argv if a.startswith("--method=")), None)
                      or ("POST" if fields else "GET"))
            hit = check_request(method, "https://api.github.com/" + path.lstrip("/"), " ".join(argv))
            if hit:
                return hit
    return None


_PROD_BRANCHES = {"main", "master", "production", "prod"}


def _deploy_command(service: str, words: list[str], flags: set[str], argv: list[str]) -> tuple[str, str] | None:
    """("live", …) for a deploy everyone sees (production), ("deploy", …) for a preview; None if it deploys nothing."""
    first = words[0] if words else ""
    second = words[1] if len(words) > 1 else ""
    if service == "vercel":
        deploy = first == "deploy" or not words or first.startswith((".", "/"))  # `vercel`, `vercel ./dist`
        preview = "--target=preview" in argv or ("--target" in argv and "preview" in argv)
        if flags & {"--prod", "--production"} or "--target=production" in argv or \
                ("--target" in argv and "production" in argv):
            return "live", "a production deploy on Vercel (the live site)"
        if deploy:  # a plain deploy can be production (a new project's first one is)
            return ("deploy", "a preview deploy on Vercel") if preview else \
                ("live", "a Vercel deploy (a new project's first one is its live site)")
        if first in ("promote", "rollback"):
            return "live", "changing what's live on Vercel"
        if first == "alias" or (first == "domains" and second == "add"):
            return "live", "pointing a domain at the site on Vercel"
        if first == "git" and second == "connect":
            return "live", "connecting the GitHub repository to Vercel (every push to main goes live)"
    if service == "netlify" and first == "deploy":
        return ("live", "a production deploy on Netlify (the live site)") if flags & {"--prod", "-p", "--prodIfUnlocked"} \
            else ("deploy", "a preview deploy on Netlify")
    if service == "firebase":
        if first == "deploy":
            return "live", "a Firebase deploy (live right away)"
        if first == "hosting:channel:deploy":
            return "deploy", "a Firebase preview channel"
    if service == "cloudflare":
        if first == "deploy":
            return "live", "a Cloudflare deploy (the live site)"
        if first == "pages" and second == "deploy":
            preview = "--branch" in flags and not ({a.split("=", 1)[-1] for a in argv} & _PROD_BRANCHES)
            return ("deploy", "a Cloudflare preview deploy") if preview else ("live", "a Cloudflare deploy (the live site)")
    if service == "railway" and first == "up":
        return "live", "a Railway deploy (live right away)"
    if service in ("eas", "expo") and first == "update":
        if {"production", "--channel=production", "--branch=production"} & set(argv):
            return "live", "an update to the app people have installed (EAS Update, production)"
        return "deploy", "an app update on a test channel (EAS Update)"
    return None


def _deploy_last(ctx: Any, agent: str) -> None:
    """Deploying comes last: not while another agent can still change the project."""
    from .sdk import ToolError

    busy = [h.name for h in ctx.running_agents() if h.id != agent and "sandbox" in (h.toolsets or [])]
    if busy:
        raise ToolError(f"Deploying comes last, and {', '.join(busy)} {'is' if len(busy) == 1 else 'are'} still "
                        "working on the project. Wait for them to finish (wait_for_agents), then deploy. If you're a "
                        "helper agent, finish and report that it's ready to deploy.")


async def approve_live(ctx: Any, agent: str, what: str, details: str = "", kind: str = "live") -> None:
    """Before any deploy. It comes last (nobody else still changing the project), and it waits for the human unless
    their launch plan says "when it's ready": once per run for going live (which also covers previews), once for
    previews only."""
    from . import launch
    from .sdk import ToolError

    _deploy_last(ctx, agent)
    plan = launch.get(ctx)
    if (plan and plan["live"] == "auto") or granted(ctx, "live", "*", agent) or \
            (kind == "deploy" and granted(ctx, "deploy", "*", agent)):
        return
    title = f"Put it live? {what}" if kind == "live" else f"Deploy a preview? {what}"
    after = ("agents put this run's work live (deploys, the domain)" if kind == "live" else
             "agents deploy previews of this run's work")
    ok, note = await ctx.request_approval(
        title, agent=agent,
        data={"kind_hint": "live", "details": (details or what)[:4000] + f"\n\nApproving lets {after} without asking "
              "again."})
    if not ok:
        raise ToolError("The human doesn't want this deployed yet. Keep it on localhost, and report that it's ready "
                        f"to deploy. {note or ''}".strip())
    grant(ctx, kind, ["*"], "*", minutes=24 * 60, note=what)


async def hold(ctx: Any, agent: str, kind: str, label: str, what: str, *, site: str | None = None) -> None:
    """Before an API call or command that buys something or speaks for the human: purchases need an approved purchase
    for the site (authorize_purchase); public actions open an approval showing exactly what will run."""
    from .sdk import ToolError

    if kind == "purchase":
        if site and granted(ctx, "purchase", site, agent):
            return
        raise ToolError(f"This is {label}, which costs money. Call authorize_purchase(amount_usd, merchant, "
                        f"description, sites=[\"{site or 'the service'}\"]) first (the human approves it), then run it "
                        "again.")
    if kind in ("live", "deploy"):
        return await approve_live(ctx, agent, label, what, kind=kind)
    if kind == "public-repo":
        from . import launch

        if launch.effective(ctx)["repo"] == "public":
            return  # the human chose a public repository in the launch plan
    if site and granted(ctx, "public", site, agent):
        return
    ok, note = await ctx.request_approval(f"Allow {label}?", agent=agent, data={"details": what[:4000]})
    if not ok:
        raise ToolError(f"The human didn't approve {label}. {note or ''}".strip())
