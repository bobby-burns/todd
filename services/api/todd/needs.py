"""Which accounts will a goal need? Read straight from the prompt, before the planner starts, so missing sign-ins
are asked for once, up-front, instead of interrupting agents later.

Two sources, both deterministic (no model call, so the check shows up the moment a run starts):
  1. Services the prompt names ("deploy to Netlify", "post it on X", "list it on Product Hunt").
  2. What the kind of work implies ("an iPhone app" → Expo, Apple Developer, App Store Connect, GitHub).
A service is left out when Todd can already use it without the browser (its CLI is connected or its API key is in
the vault). The planner can still ask for more with request_signins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import accounts, goal, vault

# Other ways people refer to catalog services. Matched case-insensitively on word boundaries.
ALIASES: dict[str, list[str]] = {
    "x": ["twitter", "x.com", "tweet", "tweets"],
    "producthunt": ["product hunt"],
    "hackernews": ["hacker news", "show hn"],
    "indiehackers": ["indie hackers"],
    "app_store_connect": ["app store connect", "testflight"],
    "apple_dev": ["apple developer"],
    "play_console": ["google play", "play store", "play console"],
    "expo": ["expo", "eas"],
    "cloudflare": ["cloudflare pages", "wrangler"],
    "gdrive": ["google drive", "google docs"],
    "devto": ["dev.to"],
    "lemonsqueezy": ["lemon squeezy"],
    "digitalocean": ["digital ocean"],
    "fly": ["fly.io"],
    "squarespace": ["squarespace", "google domains"],  # Google Domains moved to Squarespace
}
# Names that are also everyday words: only count them when written as a name (capitalized), e.g. "Notion".
PROPER_ONLY = {"notion", "linear", "render", "slack", "medium", "threads", "discord", "canva", "fly", "neon",
               "resend", "amazon", "google", "microsoft", "shopify", "substack", "figma"}
# Services that are too generic to infer from a bare name match ("Google account" from "google").
SKIP_NAME_MATCH = {"google", "microsoft", "amazon"}
HOSTS = {"vercel", "netlify", "cloudflare", "railway", "render", "fly", "heroku", "digitalocean", "firebase", "aws"}


@dataclass(frozen=True)
class Rule:
    pattern: str
    services: tuple[str, ...]
    why: str
    blurb: str  # the sign-in card's "why these accounts", in the human's terms
    needs_make: bool = True  # only when the goal is to make or ship something
    for_content: bool = False  # also when the goal is only content (a video, posts) about something that exists


# What the work implies only counts when the goal is to make or ship something ("write a report on iOS 18" isn't).
MAKE = re.compile(r"\b(build|make|create|develop|code|ship|launch|release|publish|deploy|submit|upload|put (it|them)?"
                  r"\s*(up )?on|set up|start|add|sell)\b", re.IGNORECASE)

RULES: list[Rule] = [
    Rule(r"\b(iphone|ios|ipad|app ?store|testflight|swiftui)\b",
         ("expo", "apple_dev", "app_store_connect", "github"), "iPhone app: build, sign and upload",
         "You're making an iPhone app, so Todd needs to build it, sign it and upload it to the App Store."),
    Rule(r"\b(android|google play|play store)\b", ("expo", "play_console", "github"), "Android app: build and upload",
         "You're making an Android app, so Todd needs to build it and upload it to Google Play."),
    Rule(r"\b(mobile app|react native|expo app|native app)\b", ("expo", "github"), "mobile app builds",
         "You're making a mobile app, so Todd needs somewhere to keep the code and to build the app."),
    Rule(r"\b(web ?site|web ?app|landing page|waitlist|web ?page|homepage|saas|put it online)\b",
         ("github", "vercel"), "code and hosting for the site",
         "You're making a website, so Todd needs somewhere to keep the code and to put the site online."),
    Rule(r"\b(buy|register|purchase)\b[^.\n]{0,40}\bdomain\b", ("vercel",), "buying the domain",
         "You want a domain, so Todd needs an account to buy it through.", needs_make=False, for_content=True),
    Rule(r"\b(takes?|taking|accepts?|accepting|collects?|collecting) (payments?|money)|\b(paid subscriptions?|paywall|charge (users|customers))\b|"
         r"\bsubscriptions? (with|through|via) stripe\b", ("stripe",), "payments",
         "Your goal takes payments, so Todd needs a payments account to set them up."),
    Rule(r"\b(launch (post|thread|tweet)|tweet|post (it )?on x)\b", ("x",), "posting the launch",
         "Your goal includes a launch post, so Todd needs the account to post it from.", for_content=True),
]
NAMED = "named in your goal"

# What Todd does with each account, shown on its row of the sign-in card.
USES: dict[str, str] = {
    "github": "Keeps the code and its history",
    "vercel": "Puts the site online",
    "netlify": "Puts the site online",
    "cloudflare": "Hosts the site and its DNS",
    "railway": "Runs the app's server",
    "render": "Runs the app's server",
    "fly": "Runs the app's server",
    "heroku": "Runs the app's server",
    "digitalocean": "Runs the app's server",
    "firebase": "Database, sign-in and hosting",
    "supabase": "Database and sign-in",
    "expo": "Builds the app in the cloud",
    "apple_dev": "Signs the app so iPhones will run it",
    "app_store_connect": "Uploads builds to TestFlight and the App Store",
    "play_console": "Uploads the app to Google Play",
    "stripe": "Takes the payments",
    "x": "Posts the launch",
    "producthunt": "Lists the launch",
    "squarespace": "Manages your domain and its DNS",
    "namecheap": "Manages your domain and its DNS",
    "godaddy": "Manages your domain and its DNS",
    "porkbun": "Manages your domain and its DNS",
}


def _names(sid: str, svc: accounts.Service) -> list[tuple[str, bool]]:
    """(term, case_sensitive) pairs that mean this service."""
    out: list[tuple[str, bool]] = [(a, False) for a in ALIASES.get(sid, [])]
    name = re.sub(r"\s*\(.*?\)", "", svc.name).strip()  # "Expo (EAS)" → "Expo"
    if sid not in SKIP_NAME_MATCH and len(name) >= 3:  # "X" is matched through its aliases only
        out.append((name, sid in PROPER_ONLY or name.lower() in PROPER_ONLY))
    for d in svc.domains:
        if "." in d and d not in _shared_domains():
            out.append((d, False))
    return out


def _shared_domains() -> set[str]:
    """Domains several services live on (google.com, apple.com): naming one says nothing about which service."""
    seen: dict[str, int] = {}
    for svc in accounts.catalog().values():
        for d in set(svc.domains):
            seen[d] = seen.get(d, 0) + 1
    return {d for d, n in seen.items() if n > 1}


def _mentions(text: str, term: str, case_sensitive: bool) -> bool:
    flags = 0 if case_sensitive else re.IGNORECASE
    for m in re.finditer(rf"(?<![\w.]){re.escape(term)}(?![\w])", text, flags):
        # a capital at the start of a sentence doesn't make "Render a chart" a name
        if case_sensitive and re.fullmatch(r"\s*", text[:m.start()]) or \
                case_sensitive and re.search(r"[.!?:\n]\s*$", text[:m.start()]):
            continue
        return True
    return False


def usable_without_browser(sid: str) -> bool:
    """Todd can already work with it: its CLI is signed in, or the keys its API needs are in the vault."""
    from . import connect, integrations

    if sid in ("apple_dev", "app_store_connect"):
        return all(vault.has_secret(n) for n in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"))
    if connect.is_connected(sid):
        return True
    it = integrations._find(sid)
    return bool(it and it.id == sid and it.api_secrets and all(vault.has_secret(s) for s in it.api_secrets))


def detect(prompt: str, skip_usable: bool = True) -> list[dict[str, str]]:
    """Accounts the goal will need, in a sensible order: [{"id", "name", "why"}]."""
    cat = accounts.catalog()
    found: dict[str, str] = {}
    named_hosts: set[str] = set()
    # A TikTok for an existing app needs no hosting or app-store accounts, and no TikTok account unless it's posted.
    content = goal.content_only(prompt)
    for sid, svc in cat.items():
        if svc.custom or (content and svc.category == "Social" and not goal.wants_posting(prompt)):
            continue
        if any(_mentions(prompt, term, cs) for term, cs in _names(sid, svc)):
            found.setdefault(sid, NAMED)
            if sid in HOSTS:
                named_hosts.add(sid)
    making = MAKE.search(prompt) is not None
    for rule in RULES:
        if content and not rule.for_content:
            continue
        if (making or not rule.needs_make) and re.search(rule.pattern, prompt, re.IGNORECASE):
            for sid in rule.services:
                if sid == "vercel" and named_hosts - {"vercel"}:
                    continue  # they picked another host
                found.setdefault(sid, rule.why)
    out = []
    for sid, why in found.items():
        if sid not in cat or (skip_usable and usable_without_browser(sid)):
            continue
        out.append({"id": sid, "name": cat[sid].name, "why": why, "use": USES.get(sid, "")})
    return out[:10]


def explain(wanted: list[dict[str, str]]) -> str:
    """A sentence or two for the sign-in card: why the goal needs these accounts (from what detect found)."""
    blurbs = {r.why: r.blurb for r in RULES}
    out: list[str] = []
    for w in wanted:
        b = blurbs.get(w.get("why", ""))
        if b and b not in out:
            out.append(b)
    named = [w["name"] for w in wanted if w.get("why") == NAMED]
    if named:
        names = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
        out.append(f"You mentioned {names} in your goal.")
    return " ".join(out[:3])
