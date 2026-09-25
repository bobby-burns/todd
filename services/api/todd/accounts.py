"""Accounts: which services the shared browser is signed in to.

Detection, in order of confidence:
  1. Session cookie: for services whose auth cookie is well known (e.g. GitHub `user_session`, X `auth_token`),
     read the browser's cookies over CDP. Instant, checks every service at once.
  2. Probe ("verify by visiting"): load a page that redirects to a login screen when signed out, and look at
     where it lands. Takes a few seconds per service; results are remembered.
  3. Manual: the human marks a service as signed in (e.g. apps that keep sessions in localStorage).
Services with `via` share another account's login (Firebase → Google).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from . import cdp, settings
from .sdk import ToolError, get_agent_id, get_ctx, todd_tool


@dataclass
class Service:
    id: str
    name: str
    category: str
    login: str
    check: str = ""  # page that redirects to a login screen when signed out
    domains: list[str] = field(default_factory=list)
    cookies: list[str] = field(default_factory=list)  # auth cookie names that mean "signed in"
    via: str | None = None  # uses another service's login
    custom: bool = False


def S(id, name, category, login, check="", domains=(), cookies=(), via=None) -> Service:
    if not domains:
        domains = [urlparse(login).hostname.removeprefix("www.")]  # type: ignore[union-attr]
    return Service(id, name, category, login, check or login, list(domains), list(cookies), via)


G = "https://accounts.google.com/ServiceLogin?continue="
CATALOG: list[Service] = [
    # ---- Build & deploy
    S("github", "GitHub", "Build & deploy", "https://github.com/login", "https://github.com/settings/profile",
      ["github.com"], ["user_session"]),
    S("vercel", "Vercel", "Build & deploy", "https://vercel.com/login", "https://vercel.com/dashboard"),
    S("netlify", "Netlify", "Build & deploy", "https://app.netlify.com/login", "https://app.netlify.com/",
      ["netlify.com"]),
    S("cloudflare", "Cloudflare", "Build & deploy", "https://dash.cloudflare.com/login", "https://dash.cloudflare.com/",
      ["cloudflare.com"]),
    S("railway", "Railway", "Build & deploy", "https://railway.com/login", "https://railway.com/dashboard"),
    S("render", "Render", "Build & deploy", "https://dashboard.render.com/login", "https://dashboard.render.com/",
      ["render.com"]),
    S("fly", "Fly.io", "Build & deploy", "https://fly.io/app/sign-in", "https://fly.io/dashboard"),
    S("heroku", "Heroku", "Build & deploy", "https://id.heroku.com/login", "https://dashboard.heroku.com/apps",
      ["heroku.com"]),
    S("digitalocean", "DigitalOcean", "Build & deploy", "https://cloud.digitalocean.com/login",
      "https://cloud.digitalocean.com/projects", ["digitalocean.com"]),
    S("npm", "npm", "Build & deploy", "https://www.npmjs.com/login", "https://www.npmjs.com/settings"),
    S("expo", "Expo (EAS)", "Build & deploy", "https://expo.dev/login", "https://expo.dev/settings"),
    # ---- Cloud & backend
    S("google", "Google account", "Google", "https://accounts.google.com/", "https://myaccount.google.com/",
      ["google.com"], ["SID", "__Secure-1PSID"]),
    S("firebase", "Firebase console", "Google", G + "https://console.firebase.google.com/",
      "https://console.firebase.google.com/", ["google.com"], via="google"),
    S("gcp", "Google Cloud console", "Google", G + "https://console.cloud.google.com/",
      "https://console.cloud.google.com/", ["google.com"], via="google"),
    S("gmail", "Gmail", "Google", G + "https://mail.google.com/", "https://mail.google.com/", ["google.com"],
      via="google"),
    S("gdrive", "Google Drive & Docs", "Google", G + "https://drive.google.com/", "https://drive.google.com/",
      ["google.com"], via="google"),
    S("play_console", "Google Play Console", "Google", G + "https://play.google.com/console",
      "https://play.google.com/console", ["google.com"], via="google"),
    S("search_console", "Google Search Console", "Google", G + "https://search.google.com/search-console",
      "https://search.google.com/search-console", ["google.com"], via="google"),
    S("analytics", "Google Analytics", "Google", G + "https://analytics.google.com/",
      "https://analytics.google.com/", ["google.com"], via="google"),
    S("supabase", "Supabase", "Cloud & backend", "https://supabase.com/dashboard/sign-in",
      "https://supabase.com/dashboard/projects"),
    S("aws", "AWS console", "Cloud & backend", "https://console.aws.amazon.com/",
      "https://console.aws.amazon.com/console/home", ["aws.amazon.com", "amazon.com"], ["aws-userInfo"]),
    S("azure", "Microsoft Azure", "Cloud & backend", "https://portal.azure.com/", "https://portal.azure.com/",
      ["azure.com", "microsoftonline.com"]),
    S("neon", "Neon", "Cloud & backend", "https://console.neon.tech/login", "https://console.neon.tech/app/projects",
      ["neon.tech"]),
    # ---- Domains
    S("namecheap", "Namecheap", "Domains & DNS", "https://www.namecheap.com/myaccount/login/",
      "https://ap.www.namecheap.com/dashboard", ["namecheap.com"]),
    S("porkbun", "Porkbun", "Domains & DNS", "https://porkbun.com/account/login",
      "https://porkbun.com/account/domainsSpeedy"),
    S("godaddy", "GoDaddy", "Domains & DNS", "https://sso.godaddy.com/", "https://account.godaddy.com/products",
      ["godaddy.com"]),
    # ---- Payments & commerce
    S("stripe", "Stripe", "Payments & commerce", "https://dashboard.stripe.com/login",
      "https://dashboard.stripe.com/dashboard", ["stripe.com"]),
    S("paypal", "PayPal", "Payments & commerce", "https://www.paypal.com/signin",
      "https://www.paypal.com/myaccount/summary"),
    S("shopify", "Shopify", "Payments & commerce", "https://accounts.shopify.com/lookup", "https://admin.shopify.com/",
      ["shopify.com"]),
    S("gumroad", "Gumroad", "Payments & commerce", "https://gumroad.com/login", "https://gumroad.com/dashboard"),
    S("lemonsqueezy", "Lemon Squeezy", "Payments & commerce", "https://app.lemonsqueezy.com/login",
      "https://app.lemonsqueezy.com/dashboard", ["lemonsqueezy.com"]),
    S("amazon", "Amazon", "Payments & commerce", "https://www.amazon.com/gp/css/homepage.html",
      "https://www.amazon.com/gp/css/homepage.html", ["amazon.com"], ["at-main"]),
    S("etsy", "Etsy", "Payments & commerce", "https://www.etsy.com/signin", "https://www.etsy.com/your/account"),
    S("ebay", "eBay", "Payments & commerce", "https://signin.ebay.com/", "https://www.ebay.com/mye/myebay/summary",
      ["ebay.com"]),
    # ---- Social
    S("x", "X (Twitter)", "Social", "https://x.com/i/flow/login", "https://x.com/settings/account",
      ["x.com", "twitter.com"], ["auth_token"]),
    S("linkedin", "LinkedIn", "Social", "https://www.linkedin.com/login", "https://www.linkedin.com/feed/",
      ["linkedin.com"], ["li_at"]),
    S("instagram", "Instagram", "Social", "https://www.instagram.com/accounts/login/",
      "https://www.instagram.com/accounts/edit/", ["instagram.com"], ["sessionid"]),
    S("facebook", "Facebook", "Social", "https://www.facebook.com/login", "https://www.facebook.com/settings",
      ["facebook.com"], ["c_user"]),
    S("threads", "Threads", "Social", "https://www.threads.com/login", "https://www.threads.com/settings",
      ["threads.com", "threads.net"]),
    S("tiktok", "TikTok", "Social", "https://www.tiktok.com/login", "https://www.tiktok.com/setting",
      ["tiktok.com"], ["sessionid"]),
    S("youtube", "YouTube", "Social", G + "https://www.youtube.com/", "https://studio.youtube.com/",
      ["youtube.com"], ["LOGIN_INFO"]),
    S("reddit", "Reddit", "Social", "https://www.reddit.com/login/", "https://www.reddit.com/settings/",
      ["reddit.com"], ["reddit_session"]),
    S("pinterest", "Pinterest", "Social", "https://www.pinterest.com/login/", "https://www.pinterest.com/settings/"),
    S("discord", "Discord", "Social", "https://discord.com/login", "https://discord.com/channels/@me"),
    S("twitch", "Twitch", "Social", "https://www.twitch.tv/login", "https://dashboard.twitch.tv/",
      ["twitch.tv"], ["auth-token"]),
    S("bluesky", "Bluesky", "Social", "https://bsky.app/", "https://bsky.app/settings"),
    # ---- Community & launch
    S("producthunt", "Product Hunt", "Community & launch", "https://www.producthunt.com/login",
      "https://www.producthunt.com/my/settings/edit"),
    S("hackernews", "Hacker News", "Community & launch", "https://news.ycombinator.com/login",
      "https://news.ycombinator.com/submit", ["news.ycombinator.com"], ["user"]),
    S("indiehackers", "Indie Hackers", "Community & launch", "https://www.indiehackers.com/sign-in",
      "https://www.indiehackers.com/settings"),
    S("medium", "Medium", "Community & launch", "https://medium.com/m/signin", "https://medium.com/me/settings"),
    S("substack", "Substack", "Community & launch", "https://substack.com/sign-in", "https://substack.com/settings"),
    S("devto", "DEV Community", "Community & launch", "https://dev.to/enter", "https://dev.to/settings"),
    # ---- Productivity
    S("notion", "Notion", "Productivity", "https://www.notion.so/login", "https://www.notion.so/",
      ["notion.so"], ["token_v2"]),
    S("slack", "Slack", "Productivity", "https://slack.com/signin", "https://app.slack.com/client",
      ["slack.com"], ["d"]),
    S("linear", "Linear", "Productivity", "https://linear.app/login", "https://linear.app/"),
    S("figma", "Figma", "Productivity", "https://www.figma.com/login", "https://www.figma.com/files/recents-and-sharing"),
    S("canva", "Canva", "Productivity", "https://www.canva.com/login", "https://www.canva.com/settings"),
    S("calendly", "Calendly", "Productivity", "https://calendly.com/login", "https://calendly.com/app/home"),
    S("microsoft", "Microsoft / Outlook", "Productivity", "https://login.live.com/", "https://outlook.live.com/mail/",
      ["live.com", "microsoft.com"]),
    S("dropbox", "Dropbox", "Productivity", "https://www.dropbox.com/login", "https://www.dropbox.com/home"),
    # ---- Email & marketing
    S("resend", "Resend", "Email & marketing", "https://resend.com/login", "https://resend.com/emails"),
    S("beehiiv", "beehiiv", "Email & marketing", "https://app.beehiiv.com/login", "https://app.beehiiv.com/",
      ["beehiiv.com"]),
    S("mailchimp", "Mailchimp", "Email & marketing", "https://login.mailchimp.com/", "https://admin.mailchimp.com/",
      ["mailchimp.com"]),
    # ---- App stores
    S("apple_dev", "Apple Developer", "App stores", "https://developer.apple.com/account",
      "https://developer.apple.com/account", ["apple.com"], ["myacinfo"]),
    S("app_store_connect", "App Store Connect", "App stores", "https://appstoreconnect.apple.com/",
      "https://appstoreconnect.apple.com/apps", ["apple.com"], via="apple_dev"),
    # ---- AI platforms
    S("openai", "OpenAI Platform", "AI platforms", "https://platform.openai.com/login",
      "https://platform.openai.com/settings/organization/general", ["openai.com"]),
    S("anthropic", "Anthropic Console", "AI platforms", "https://console.anthropic.com/login",
      "https://console.anthropic.com/settings/keys", ["anthropic.com"]),
    S("huggingface", "Hugging Face", "AI platforms", "https://huggingface.co/login",
      "https://huggingface.co/settings/profile"),
    S("replicate", "Replicate", "AI platforms", "https://replicate.com/signin", "https://replicate.com/account"),
]

LOGIN_MARKERS = ("login", "signin", "sign-in", "sign_in", "/auth", "sso.", "/sso", "servicelogin", "/enter",
                 "/lookup", "identifier", "session/new", "/account/begin", "accounts.google.com")
NO_PROBE = {"bluesky"}  # single-page apps that show a login modal without changing the URL
_cookie_cache: tuple[float, list[dict[str, Any]]] | None = None


def catalog() -> dict[str, Service]:
    out = {s.id: s for s in CATALOG}
    for c in settings.get("accounts_custom") or []:
        try:
            svc = S(c["id"], c["name"], "Custom", c["login"], c.get("check") or c["login"],
                    c.get("domains") or [], c.get("cookies") or [])
            svc.custom = True
            out[svc.id] = svc
        except Exception:
            continue
    return out


def _matches(cookie_domain: str, domain: str) -> bool:
    cd, d = cookie_domain.lstrip(".").lower(), domain.lstrip(".").lower()
    return cd == d or cd.endswith("." + d) or d.endswith("." + cd)


async def _cookies(max_age: float = 3.0) -> list[dict[str, Any]] | None:
    global _cookie_cache
    now = time.monotonic()
    if _cookie_cache and now - _cookie_cache[0] < max_age:
        return _cookie_cache[1]
    try:
        cookies = await cdp.get_cookies()
    except Exception:
        return None
    _cookie_cache = (now, cookies)
    return cookies


def _cookie_verdict(svc: Service, cookies: list[dict[str, Any]]) -> tuple[bool | None, float | None, int]:
    now = time.time()
    on_domain = [c for c in cookies if any(_matches(c.get("domain", ""), d) for d in svc.domains)]
    if not svc.cookies:
        return None, None, len(on_domain)
    live = [c for c in on_domain if c.get("name") in svc.cookies and c.get("value")
            and (c.get("expires", -1) in (-1, 0) or c.get("expires", 0) > now)]
    exp = max((c.get("expires", -1) for c in live), default=None)
    return (bool(live), exp if exp and exp > 0 else None, len(on_domain))


def is_login_url(url: str) -> bool:
    u = url.lower()
    return any(m in u for m in LOGIN_MARKERS)


async def statuses(ids: list[str] | None = None) -> dict[str, Any]:
    cat = catalog()
    cookies = await _cookies()
    manual: dict[str, Any] = settings.get("accounts_manual") or {}
    selected = set(settings.get("accounts_selected") or [])
    wanted = [cat[i] for i in ids if i in cat] if ids else list(cat.values())
    results: dict[str, dict[str, Any]] = {}

    def one(svc: Service) -> dict[str, Any]:
        if svc.id in results:
            return results[svc.id]
        row: dict[str, Any] = {**asdict(svc), "selected": svc.id in selected}
        if svc.via and svc.via in cat:
            parent = one(cat[svc.via])
            row.update(status=parent["status"], source=f"via {cat[svc.via].name}", expires=parent.get("expires"),
                       checked_at=parent.get("checked_at"), cookie_count=parent.get("cookie_count"))
            results[svc.id] = row
            return row
        verdict, expires, count = _cookie_verdict(svc, cookies) if cookies is not None else (None, None, 0)
        row["cookie_count"] = count
        m = manual.get(svc.id)
        if verdict is not None:
            row.update(status="signed_in" if verdict else "signed_out", source="session cookie", expires=expires)
        elif m:
            row.update(status="signed_in" if m.get("signed_in") else "signed_out",
                       source="verified by visit" if m.get("method") == "probe" else "marked by you",
                       checked_at=m.get("at"))
        else:
            row.update(status="unknown", source="not checked yet" if count else "no session for this site")
            if count == 0 and cookies is not None:
                row["status"] = "signed_out"
        if cookies is None:
            row.update(status="unknown", source="browser offline")
        results[svc.id] = row
        return row

    for svc in wanted:
        one(svc)
    return {"browser_online": cookies is not None, "accounts": [results[s.id] for s in wanted]}


def _remember(service_id: str, signed_in: bool, method: str) -> None:
    manual = dict(settings.get("accounts_manual") or {})
    manual[service_id] = {"signed_in": signed_in, "method": method, "at": datetime.now(timezone.utc).isoformat()}
    settings.update({"accounts_manual": manual})


def forget(service_id: str) -> None:
    manual = dict(settings.get("accounts_manual") or {})
    manual.pop(service_id, None)
    settings.update({"accounts_manual": manual})


async def verify(service_id: str) -> dict[str, Any]:
    """Probe: load the service's check URL and see whether it lands on a login page."""
    cat = catalog()
    svc = cat.get(service_id)
    if not svc:
        raise KeyError(service_id)
    target = cat[svc.via] if svc.via and svc.via in cat else svc
    if target.id in NO_PROBE:
        return {"id": service_id, "final_url": None, "signed_in": None,
                "note": f"{target.name} can't be verified by visiting; mark it signed in yourself."}
    page = await cdp.probe(target.check)
    final = page.get("url") or ""
    if not final or page.get("error") or final.startswith(("chrome-error:", "about:", "chrome:")):
        return {"id": service_id, "final_url": final, "signed_in": None,
                "note": f"Couldn't load {target.check} (offline or blocked); status unchanged."}
    # Signed out if we were redirected to a login URL, or the page shows a password field (SPA login screens).
    signed_in = not is_login_url(final) and not is_login_url(urlparse(final).netloc + "/") and not page.get("password")
    _remember(target.id, signed_in, "probe")
    global _cookie_cache
    _cookie_cache = None
    return {"id": service_id, "final_url": final, "signed_in": signed_in}


async def open_login(service_id: str) -> str:
    svc = catalog().get(service_id)
    if not svc:
        raise KeyError(service_id)
    forget(svc.via or svc.id)  # a fresh sign-in supersedes an old manual/probe result
    return await cdp.open_tab(svc.login)


async def sign_out(service_id: str) -> int:
    cat = catalog()
    svc = cat.get(service_id)
    if not svc:
        raise KeyError(service_id)
    target = cat[svc.via] if svc.via and svc.via in cat else svc
    n = await cdp.delete_cookies(target.domains)
    _remember(target.id, False, "manual")
    global _cookie_cache
    _cookie_cache = None
    return n


def mark(service_id: str, signed_in: bool) -> None:
    _remember(service_id, signed_in, "manual")


def _find(query: str, cat: dict[str, Service]) -> Service | None:
    q = query.strip().lower()
    if q in cat:
        return cat[q]
    for s in cat.values():
        if q == s.name.lower() or q in [d.lower() for d in s.domains] or s.name.lower().startswith(q):
            return s
    for s in cat.values():
        if q in s.name.lower():
            return s
    return None


# ------------------------------------------------------------------------------------------- agent tools
@todd_tool(toolset="accounts", planner=True)
async def check_accounts(services: list[str] | None = None, verify_unknown: bool = True) -> dict:
    """Check which services the shared browser is signed in to (e.g. ["github", "vercel", "google", "x"]).
    Call this at the start of a run for every service the work will need, so missing sign-ins can be fixed
    up-front with request_signins instead of interrupting agents later. Omit services to list all accounts the
    human set up.

    Args:
        services: service ids or names (e.g. "firebase", "Product Hunt", "stripe.com")
        verify_unknown: visit pages to verify services whose status is unknown (a few seconds each)
    """
    cat = catalog()
    if services:
        found, missing = [], []
        for q in services:
            s = _find(q, cat)
            (found.append(s.id) if s else missing.append(q))
        ids = found
    else:
        ids, missing = list(settings.get("accounts_selected") or []), []
        if not ids:
            return {"note": "The human hasn't picked accounts yet. Pass the services you need.", "accounts": []}
    st = await statuses(ids)
    if not st["browser_online"]:
        return {"browser_online": False, "note": "The browser is offline; account status unknown."}
    if verify_unknown:
        unknown = [a["id"] for a in st["accounts"] if a["status"] == "unknown"][:8]
        if unknown:
            await asyncio.gather(*(verify(i) for i in unknown), return_exceptions=True)
            st = await statuses(ids)
    out = [{"id": a["id"], "name": a["name"], "status": a["status"], "how": a["source"]} for a in st["accounts"]]
    res: dict[str, Any] = {"accounts": out}
    if missing:
        res["not_in_catalog"] = missing
        res["note"] = "Services not in the catalog can still be used; ask the human if they're signed in."
    return res


@todd_tool(toolset="accounts", planner=True)
async def request_signins(services: list[str], reason: str = "") -> dict:
    """Ask the human to sign in to several services at once (opens each login page in the live browser), wait
    for them, then re-check. Use right after check_accounts, before starting work that needs those accounts.

    Args:
        services: service ids/names that need a sign-in
        reason: one line on why these accounts are needed
    """
    ctx = get_ctx()
    cat = catalog()
    svcs = [s for s in (_find(q, cat) for q in services) if s]
    if not svcs:
        raise ToolError("none of those services are in the catalog; ask_human instead")
    for s in svcs[:6]:
        try:
            await open_login(s.id)
        except Exception:
            pass
    names = ", ".join(s.name for s in svcs)
    await ctx.ask_human(
        f"Please sign in to: {names}. Login pages are open in the live browser (or use the Accounts page). "
        f"Reply 'done' when finished." + (f"\nWhy: {reason}" if reason else ""),
        agent=get_agent_id(), data={"services": [s.id for s in svcs], "kind_hint": "signin"})
    return await check_accounts.ainvoke({"services": [s.id for s in svcs]})


ACCOUNT_TOOLS = [check_accounts, request_signins]
