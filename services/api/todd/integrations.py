"""API-first routing: for a service, what's the best non-browser way to work with it?

`find_integrations` tells an agent, per service, in order of preference:
  1. a toolset that's ready right now (built-in API toolset, plugin, or configured MCP server)
  2. an official MCP server the human could add in one click
  3. a REST API usable via `api_request` (if its key is in the vault) or a CLI in the sandbox
  4. the browser, only when none of the above exist or would need setup the human hasn't done
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import settings, vault
from .sdk import get_ctx, todd_tool


@dataclass
class Integration:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    toolset: str | None = None  # built-in toolset covering it
    api_docs: str | None = None
    api_secrets: list[str] = field(default_factory=list)  # vault names that make the API usable
    cli: str | None = None  # CLI available in the sandbox (or installable with npx)
    mcp_url: str | None = None  # official remote MCP server
    mcp_docs: str | None = None
    browser_note: str | None = None  # when the browser is still the practical route


I = Integration
CATALOG: list[Integration] = [
    I("github", "GitHub", ["gh"], toolset="github", api_docs="https://docs.github.com/rest", api_secrets=["GITHUB_TOKEN"],
      cli="git (sandbox; git_push handles auth)", mcp_url="https://api.githubcopilot.com/mcp/",
      mcp_docs="https://github.com/github/github-mcp-server"),
    I("vercel", "Vercel", [], toolset="vercel", api_docs="https://vercel.com/docs/rest-api", api_secrets=["VERCEL_TOKEN"],
      cli="npx vercel (sandbox)", mcp_url="https://mcp.vercel.com", mcp_docs="https://vercel.com/docs/mcp"),
    I("stripe", "Stripe", [], api_docs="https://docs.stripe.com/api", api_secrets=["STRIPE_SECRET_KEY"],
      cli="npx stripe (sandbox)", mcp_url="https://mcp.stripe.com", mcp_docs="https://docs.stripe.com/mcp"),
    I("supabase", "Supabase", [], api_docs="https://supabase.com/docs/reference/api",
      api_secrets=["SUPABASE_ACCESS_TOKEN"], cli="npx supabase (sandbox)", mcp_url="https://mcp.supabase.com/mcp",
      mcp_docs="https://supabase.com/docs/guides/getting-started/mcp"),
    I("firebase", "Firebase", ["google firebase"], api_docs="https://firebase.google.com/docs/projects/api/reference/rest",
      api_secrets=["GOOGLE_APPLICATION_CREDENTIALS_JSON"], cli="firebase (sandbox; `firebase mcp` also runs an MCP server)",
      browser_note="Without a service-account key, creating projects/apps is quickest in the console via the browser."),
    I("cloudflare", "Cloudflare", ["wrangler"], api_docs="https://developers.cloudflare.com/api/",
      api_secrets=["CLOUDFLARE_API_TOKEN"], cli="npx wrangler (sandbox)",
      mcp_docs="https://developers.cloudflare.com/agents/model-context-protocol/"),
    I("netlify", "Netlify", [], api_docs="https://docs.netlify.com/api/get-started/", api_secrets=["NETLIFY_AUTH_TOKEN"],
      cli="npx netlify-cli (sandbox)"),
    I("neon", "Neon", [], api_docs="https://api-docs.neon.tech/reference/getting-started-with-neon-api",
      api_secrets=["NEON_API_KEY"], mcp_url="https://mcp.neon.tech/mcp"),
    I("linear", "Linear", [], api_docs="https://developers.linear.app/docs/graphql/working-with-the-graphql-api",
      api_secrets=["LINEAR_API_KEY"], mcp_url="https://mcp.linear.app/mcp", mcp_docs="https://linear.app/docs/mcp"),
    I("notion", "Notion", [], api_docs="https://developers.notion.com/reference/intro", api_secrets=["NOTION_TOKEN"],
      mcp_url="https://mcp.notion.com/mcp", mcp_docs="https://developers.notion.com/docs/mcp"),
    I("sentry", "Sentry", [], api_docs="https://docs.sentry.io/api/", api_secrets=["SENTRY_AUTH_TOKEN"],
      mcp_url="https://mcp.sentry.dev/mcp"),
    I("figma", "Figma", [], api_docs="https://www.figma.com/developers/api", api_secrets=["FIGMA_TOKEN"],
      mcp_url="https://mcp.figma.com/mcp"),
    I("slack", "Slack", [], api_docs="https://api.slack.com/methods", api_secrets=["SLACK_BOT_TOKEN"]),
    I("resend", "Resend", ["email"], api_docs="https://resend.com/docs/api-reference", api_secrets=["RESEND_API_KEY"]),
    I("porkbun", "Porkbun", [], api_docs="https://porkbun.com/api/json/v3/documentation",
      api_secrets=["PORKBUN_API_KEY", "PORKBUN_SECRET_KEY"]),
    I("namecheap", "Namecheap", [], api_docs="https://www.namecheap.com/support/api/intro/",
      api_secrets=["NAMECHEAP_API_KEY"], browser_note="The API needs your IP allow-listed; the browser is fine for one-offs."),
    I("app_store_connect", "App Store Connect", ["apple", "app store", "testflight", "asc"],
      api_docs="https://developer.apple.com/documentation/appstoreconnectapi",
      api_secrets=["ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"], cli="fastlane / a JWT script in the sandbox",
      browser_note="Without an App Store Connect API key, use the browser (the human is signed in)."),
    I("google_play", "Google Play Console", ["play console", "android"],
      api_docs="https://developers.google.com/android-publisher", api_secrets=["GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"],
      browser_note="Without a service account, use the browser."),
    I("x", "X (Twitter)", ["twitter"], api_docs="https://docs.x.com/x-api", api_secrets=["X_BEARER_TOKEN"],
      browser_note="Posting via API needs a paid tier; otherwise post through the browser (with approval)."),
    I("linkedin", "LinkedIn", [], api_docs="https://learn.microsoft.com/linkedin/",
      browser_note="Posting APIs need an approved app; use the browser (with approval)."),
    I("tiktok", "TikTok", [], api_docs="https://developers.tiktok.com/doc/content-posting-api-get-started",
      browser_note="The Content Posting API needs app review; use the browser (with approval)."),
    I("instagram", "Instagram", ["meta"], api_docs="https://developers.facebook.com/docs/instagram-platform",
      api_secrets=["INSTAGRAM_ACCESS_TOKEN"], browser_note="Graph API needs a Business account + app; else the browser."),
    I("reddit", "Reddit", [], api_docs="https://www.reddit.com/dev/api", api_secrets=["REDDIT_CLIENT_ID"]),
    I("producthunt", "Product Hunt", ["ph"], api_docs="https://api.producthunt.com/v2/docs",
      browser_note="The API is read-mostly; scheduling a launch is done in the browser."),
    I("shopify", "Shopify", [], api_docs="https://shopify.dev/docs/api/admin-graphql",
      api_secrets=["SHOPIFY_ADMIN_TOKEN"], cli="npx @shopify/cli (sandbox)"),
    I("openai", "OpenAI", [], api_docs="https://platform.openai.com/docs/api-reference", api_secrets=["OPENAI_API_KEY"]),
    I("anthropic", "Anthropic", ["claude"], api_docs="https://docs.claude.com/en/api/overview",
      api_secrets=["ANTHROPIC_API_KEY"]),
]


def _find(q: str) -> Integration | None:
    q = q.strip().lower()
    for it in CATALOG:
        if q in (it.id, it.name.lower()) or q in it.aliases:
            return it
    for it in CATALOG:
        if q in it.name.lower() or it.id in q:
            return it
    return None


def _configured_mcp(it: Integration) -> str | None:
    servers = settings.get("mcp_servers") or {}
    for name, conn in servers.items():
        url = (conn or {}).get("url", "") if isinstance(conn, dict) else ""
        if name.lower() == it.id or (it.mcp_url and url.rstrip("/") == it.mcp_url.rstrip("/")):
            return f"mcp_{name}"
    return None


def assess(query: str, available_toolsets: set[str] | None = None) -> dict[str, Any]:
    it = _find(query)
    if it is None:
        return {"service": query, "known": False, "recommendation": "Not in the catalog. Search its docs with "
                "fetch_url for a REST API or MCP server before considering the browser."}
    toolsets = available_toolsets or set()
    have = [s for s in it.api_secrets if vault.has_secret(s)]
    api_ready = bool(it.api_secrets) and len(have) == len(it.api_secrets)
    mcp_ts = _configured_mcp(it)
    out: dict[str, Any] = {"service": it.name, "known": True}
    if it.toolset and it.toolset in toolsets and (api_ready or not it.api_secrets):
        rec = f"Use the `{it.toolset}` toolset (API, ready)."
        route = "toolset"
    elif it.id in toolsets:  # a plugin toolset named after the service
        rec = f"Use the `{it.id}` toolset (plugin, ready)."
        route = "toolset"
    elif mcp_ts and mcp_ts in toolsets:
        rec = f"Use the `{mcp_ts}` toolset (MCP server, ready)."
        route = "mcp"
    elif api_ready:
        rec = ("Use `api_request` against its REST API with " + ", ".join(f"{{{{secret:{s}}}}}" for s in it.api_secrets)
               + (f", or `{it.cli}` in the sandbox" if it.cli else "") + f". Docs: {it.api_docs}")
        route = "api"
    elif it.mcp_url:
        rec = (f"An official MCP server exists ({it.mcp_url}). If you'll use {it.name} more than once, ask the human "
               f"to add it (Settings → Toolsets, one click). For a one-off, the browser is acceptable.")
        route = "mcp_available"
    elif it.cli and not it.api_secrets:
        rec = f"Use `{it.cli}`."
        route = "cli"
    else:
        rec = it.browser_note or (f"No usable API credentials ({', '.join(it.api_secrets)} not in the vault). "
                                  "Use the browser, or ask the human for a key if this will be repeated.")
        route = "browser"
    out.update(route=route, recommendation=rec, api_docs=it.api_docs, cli=it.cli, mcp_url=it.mcp_url,
               api_keys_in_vault=have, api_keys_needed=it.api_secrets)
    if it.browser_note and route == "browser":
        out["browser_note"] = it.browser_note
    return out


def catalog() -> list[dict[str, Any]]:
    out = []
    for it in CATALOG:
        d = asdict(it)
        d["mcp_configured"] = _configured_mcp(it) is not None
        d["api_keys_in_vault"] = [s for s in it.api_secrets if vault.has_secret(s)]
        out.append(d)
    return out


@todd_tool
async def find_integrations(services: list[str]) -> dict:
    """Before touching the browser, look up the best API-first way to work with each service: a ready toolset,
    a configured MCP server, a REST API with a key in the vault (use api_request), or a sandbox CLI. The browser
    is the last resort. Call this at the start of any work involving an external service.

    Args:
        services: service names, e.g. ["stripe", "app store connect", "tiktok"]
    """
    try:
        available = set(getattr(get_ctx(), "toolsets", {}) or {})
    except RuntimeError:
        available = set()
    return {"services": [assess(s, available) for s in services]}
