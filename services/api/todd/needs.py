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

from . import accounts, vault

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
    needs_make: bool = True  # only when the goal is to make or ship something


# What the work implies only counts when the goal is to make or ship something ("write a report on iOS 18" isn't).
MAKE = re.compile(r"\b(build|make|create|develop|code|ship|launch|release|publish|deploy|submit|upload|put (it|them)?"
                  r"\s*(up )?on|set up|start|add|sell)\b", re.IGNORECASE)

RULES: list[Rule] = [
    Rule(r"\b(iphone|ios|ipad|app ?store|testflight|swiftui)\b",
         ("expo", "apple_dev", "app_store_connect", "github"), "iPhone app: build, sign and upload"),
    Rule(r"\b(android|google play|play store)\b", ("expo", "play_console", "github"), "Android app: build and upload"),
    Rule(r"\b(mobile app|react native|expo app|native app)\b", ("expo", "github"), "mobile app builds"),
    Rule(r"\b(web ?site|web ?app|landing page|waitlist|web ?page|homepage|saas|put it online)\b",
         ("github", "vercel"), "code and hosting for the site"),
    Rule(r"\b(buy|register|purchase)\b[^.\n]{0,40}\bdomain\b", ("vercel",), "buying the domain",
         needs_make=False),
    Rule(r"\b(takes?|taking|accepts?|accepting|collects?|collecting) (payments?|money)|\b(paid subscriptions?|paywall|charge (users|customers))\b|"
         r"\bsubscriptions? (with|through|via) stripe\b", ("stripe",), "payments"),
    Rule(r"\b(launch (post|thread|tweet)|tweet|post (it )?on x)\b", ("x",), "posting the launch"),
]


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
    for sid, svc in cat.items():
        if svc.custom:
            continue
        if any(_mentions(prompt, term, cs) for term, cs in _names(sid, svc)):
            found.setdefault(sid, "named in your goal")
            if sid in HOSTS:
                named_hosts.add(sid)
    making = MAKE.search(prompt) is not None
    for rule in RULES:
        if (making or not rule.needs_make) and re.search(rule.pattern, prompt, re.IGNORECASE):
            for sid in rule.services:
                if sid == "vercel" and named_hosts - {"vercel"}:
                    continue  # they picked another host
                found.setdefault(sid, rule.why)
    out = []
    for sid, why in found.items():
        if sid not in cat or (skip_usable and usable_without_browser(sid)):
            continue
        out.append({"id": sid, "name": cat[sid].name, "why": why})
    return out[:10]
