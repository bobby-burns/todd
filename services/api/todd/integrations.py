"""API-first routing: for a service, what's the best non-browser way to work with it?

`find_integrations` tells an agent, per service, in order of preference:
  1. a toolset that's ready right now (built-in API toolset, plugin, or configured MCP server)
  2. a REST API usable via `api_request` (if its key is in the vault)
  3. its CLI signed in with the human's account (`cli`), or connecting it with their browser session (`cli_login`)
  4. an official MCP server the human could add in one click, or a CLI in the sandbox
  5. the browser, only when none of the above exist or would need setup the human hasn't done
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
    tool: str | None = None  # a sandbox tool that signs in with api_secrets itself (e.g. eas with EXPO_TOKEN)
    setup: str | None = None  # how an agent creates the api_secrets in the browser once the human is signed in


# Squarespace Domains (where Google Domains went) has no API for domains or DNS: agents use the browser.
SQUARESPACE_DNS = """\
Squarespace has no API for domains or DNS, so this is browser work (the human signs in once: check_accounts /
request_signins with "squarespace").
- Domains list: https://account.squarespace.com/domains. DNS for one domain:
  https://account.squarespace.com/domains/managed/<domain>/dns/dns-settings ("Custom records" → Add record).
- Point it at Vercel: first vercel_add_domain (or the host's own "add domain"), then add exactly the records the host
  asks for. Vercel's usual ones: A record, host @, data 76.76.21.21; CNAME record, host www, data cname.vercel-dns.com.
  Netlify / Cloudflare Pages / others: use the records their dashboard or CLI prints for the domain.
- Remove conflicting records first (Squarespace's default "Squarespace Defaults" A/CNAME records, or parking records)
  or the new ones won't take effect; ask the human before removing anything that serves email (MX, TXT/SPF, DKIM).
- Changes take minutes to a few hours; check with vercel_domain_status or `dig` in the sandbox, don't re-add records.
- Buying a domain here is a card checkout (browser_start with payment_*), so prefer vercel_buy_domain unless the
  human wants it at Squarespace."""


# One-time key setup in the browser (the human only signs in). Written for both browser engines: the direct tools'
# names first, the browse() agent's actions in parentheses.
ASC_SETUP = """\
Needs the human signed in to App Store Connect as Account Holder or Admin (check_accounts / request_signins).
1. Open https://appstoreconnect.apple.com/access/integrations/api (browser_start with why_not_api="Apple's API can't
   create its own keys", or browse with that start_url).
2. If it asks to Request Access to the App Store Connect API (a team's first time), that accepts Apple's terms:
   request_approval first, then click Request Access and accept.
3. On Team Keys, click Generate API Key (or +). Name: Todd. Access: App Manager. Generate.
4. Save the Issuer ID (above the keys table) as ASC_ISSUER_ID and the new key's Key ID as ASC_KEY_ID with
   browser_save_secret (browse: save_to_vault). Point at the value or its Copy button.
5. Click Download on the new key's row and, in the confirmation, use browser_save_download (browse:
   save_download_to_vault) on the final Download button with name ASC_PRIVATE_KEY. Apple allows one download per key:
   if it's gone, generate another key instead of asking the human.
6. Optional: on https://developer.apple.com/account (Membership details), save the Team ID as APPLE_TEAM_ID.
7. Finish (browser_done). Never read, copy or repeat the key yourself."""

I = Integration
CATALOG: list[Integration] = [
    I("github", "GitHub", ["gh"], toolset="github", api_docs="https://docs.github.com/rest", api_secrets=["GITHUB_TOKEN"],
      cli="gh and git (sandbox: the `git`, `git_push` and `gh` tools handle auth; plain git in shell has none)", mcp_url="https://api.githubcopilot.com/mcp/",
      mcp_docs="https://github.com/github/github-mcp-server"),
    I("vercel", "Vercel", [], toolset="vercel", api_docs="https://vercel.com/docs/rest-api", api_secrets=["VERCEL_TOKEN"],
      cli="the Vercel CLI (sandbox: cli(\"vercel\", …))", mcp_url="https://mcp.vercel.com", mcp_docs="https://vercel.com/docs/mcp"),
    I("stripe", "Stripe", [], api_docs="https://docs.stripe.com/api", api_secrets=["STRIPE_SECRET_KEY"],
      cli="the Stripe CLI (sandbox: cli(\"stripe\", …))", mcp_url="https://mcp.stripe.com", mcp_docs="https://docs.stripe.com/mcp"),
    I("supabase", "Supabase", [], api_docs="https://supabase.com/docs/reference/api",
      api_secrets=["SUPABASE_ACCESS_TOKEN"], cli="npx supabase (sandbox)", mcp_url="https://mcp.supabase.com/mcp",
      mcp_docs="https://supabase.com/docs/guides/getting-started/mcp"),
    I("firebase", "Firebase", ["google firebase"], api_docs="https://firebase.google.com/docs/projects/api/reference/rest",
      api_secrets=["GOOGLE_APPLICATION_CREDENTIALS_JSON"],
      cli="the Firebase CLI (sandbox: cli(\"firebase\", …); `firebase mcp` also runs an MCP server)",
      browser_note="Without a service-account key, creating projects/apps is quickest in the console via the browser."),
    I("cloudflare", "Cloudflare", ["wrangler"], api_docs="https://developers.cloudflare.com/api/",
      api_secrets=["CLOUDFLARE_API_TOKEN"], cli="Wrangler (sandbox: cli(\"cloudflare\", …))",
      mcp_docs="https://developers.cloudflare.com/agents/model-context-protocol/"),
    I("netlify", "Netlify", [], api_docs="https://docs.netlify.com/api/get-started/", api_secrets=["NETLIFY_AUTH_TOKEN"],
      cli="the Netlify CLI (sandbox: cli(\"netlify\", …))"),
    I("railway", "Railway", [], api_docs="https://docs.railway.com/reference/public-api", api_secrets=["RAILWAY_TOKEN"],
      cli="the Railway CLI (sandbox: cli(\"railway\", …))"),
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
    I("squarespace", "Squarespace Domains", ["squarespace", "google domains", "squarespace domains"],
      browser_note=SQUARESPACE_DNS),
    I("namecheap", "Namecheap", [], api_docs="https://www.namecheap.com/support/api/intro/",
      api_secrets=["NAMECHEAP_API_KEY"], browser_note="The API needs your IP allow-listed; the browser is fine for one-offs."),
    I("expo", "Expo (EAS)", ["eas", "expo application services", "react native", "expo go", "ios", "iphone",
                             "mobile app"],
      api_docs="https://docs.expo.dev/eas/", api_secrets=["EXPO_TOKEN"], tool="eas",
      cli="the EAS CLI (sandbox: eas(…), signed in with your Expo account; builds run on Expo's servers, macOS included)",
      browser_note="Connect it with cli_login(\"expo\") (the browser's Expo session approves the EAS CLI sign-in)."),
    I("app_store_connect", "App Store Connect", ["apple", "app store", "testflight", "asc"],
      api_docs="https://developer.apple.com/documentation/appstoreconnectapi",
      api_secrets=["ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"],
      cli="eas(\"submit -p ios …\") uploads builds with this key; for other API calls, a JWT script in the sandbox",
      browser_note="Without an App Store Connect API key, use the browser (the human is signed in). The API can't "
                   "create a new app record: the human's first interactive `npx eas-cli build -p ios --auto-submit` "
                   "does it, or one form in the browser (My Apps → + → New App).",
      setup=ASC_SETUP),
    I("google_play", "Google Play Console", ["play console", "android"],
      api_docs="https://developers.google.com/android-publisher", api_secrets=["GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"],
      browser_note="Without a service account, use the browser. Google requires the very first .aab of a new app "
                   "to be uploaded by hand in the Play Console; eas submit works for later ones."),
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
    I("pixabay", "Pixabay", ["stock photos", "stock images", "free images"], toolset="video",
      api_docs="https://pixabay.com/api/docs/", api_secrets=["PIXABAY_API_KEY"],
      browser_note="Needs a free API key: the human signs up at https://pixabay.com/api/docs/ and the key is shown "
                   "on that page. Ask for it with ask_human(..., secret_name=\"PIXABAY_API_KEY\"). Without it, the "
                   "`video` toolset can still use the run's own images (sources=[\"workspace\"])."),
    I("pexels", "Pexels", [], toolset="video",
      api_docs="https://www.pexels.com/api/documentation/", api_secrets=["PEXELS_API_KEY"],
      browser_note="Pexels has paused new API keys: an existing PEXELS_API_KEY still works, otherwise use Pixabay "
                   "(a free PIXABAY_API_KEY from https://pixabay.com/api/docs/) or the run's own images "
                   "(sources=[\"workspace\"])."),
    I("apify", "Apify", ["tiktok trends", "trending videos", "viral videos"], toolset="trends",
      api_docs="https://docs.apify.com/api/v2", api_secrets=["APIFY_API_TOKEN"],
      browser_note="Needs an API token: the human finds it at https://console.apify.com/settings/integrations (the "
                   "free plan includes $5 a month). Ask for it with ask_human(..., secret_name=\"APIFY_API_TOKEN\")."),
    I("elevenlabs", "ElevenLabs", ["voiceover", "voice over", "text to speech", "tts"], toolset="shorts",
      api_docs="https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps",
      api_secrets=["ELEVENLABS_API_KEY"],
      browser_note="Needs an API key: the human makes one at https://elevenlabs.io/app/settings/api-keys (the free "
                   "plan works for trying it; commercial use needs a paid plan). Ask for it with "
                   "ask_human(..., secret_name=\"ELEVENLABS_API_KEY\")."),
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


def _needs_connect(it: Integration) -> bool:
    from . import connect

    return (it.id in connect.CONNECTORS and not connect.is_connected(it.id)
            and not all(vault.has_secret(s) for s in it.api_secrets))


def assess(query: str, available_toolsets: set[str] | None = None,
           browser_status: dict[str, str] | None = None) -> dict[str, Any]:
    """browser_status: service id -> "signed_in" / "signed_out" / "unknown" (from the accounts check)."""
    it = _find(query)
    if it is None:
        return {"service": query, "known": False, "recommendation": "Not in the catalog. Search its docs with "
                "fetch_url for a REST API or MCP server before considering the browser."}
    toolsets = available_toolsets or set()
    have = [s for s in it.api_secrets if vault.has_secret(s)]
    api_ready = bool(it.api_secrets) and len(have) == len(it.api_secrets)
    mcp_ts = _configured_mcp(it)
    from . import connect

    conn = connect.CONNECTORS.get(it.id)
    out: dict[str, Any] = {"service": it.name, "known": True}
    if it.toolset and it.toolset in toolsets and (api_ready or not it.api_secrets):
        rec = f"Use the `{it.toolset}` toolset (API, ready)."
        route = "toolset"
    elif it.id in toolsets and it.id != it.toolset:  # a plugin toolset named after the service
        rec = f"Use the `{it.id}` toolset (plugin, ready)."
        route = "toolset"
    elif mcp_ts and mcp_ts in toolsets:
        rec = f"Use the `{mcp_ts}` toolset (MCP server, ready)."
        route = "mcp"
    elif it.tool and (api_ready or (conn is not None and connect.is_connected(it.id))):
        rec = (f"Use `{it.tool}(…)` in the sandbox toolset: it's signed in with the human's account already (no login "
               f"step, never pass a token yourself). Docs: {it.api_docs}")
        route = "cli"
    elif it.setup and it.api_secrets and not api_ready:
        rec = (f"{it.name}'s keys ({', '.join(it.api_secrets)}) aren't in the vault yet, and Todd sets them up itself: "
               "before other work, spawn one short agent (e.g. \"Setup: " + it.name + " key\") with the `browser` "
               "and `accounts` toolsets and these steps as its task, then wait for it:\n" + it.setup)
        route = "setup"
    elif it.tool and not _needs_connect(it):
        missing = [s for s in it.api_secrets if not vault.has_secret(s)]
        rec = (f"`{it.tool}(…)` in the sandbox toolset needs {', '.join(missing)} in the vault. Ask the human "
               "(ask_human) to add it in Settings → Vault, then continue. " + (it.browser_note or ""))
        route = "needs_key"
    elif api_ready:
        rec = ("Use `api_request` against its REST API with " + ", ".join(f"{{{{secret:{s}}}}}" for s in it.api_secrets)
               + (f", or `{it.cli}` in the sandbox" if it.cli else "") + f". Docs: {it.api_docs}")
        route = "api"
    elif conn is not None and not conn.secret and connect.is_connected(it.id):
        rec = (f"The {conn.name} is signed in with the human's account: cli(\"{it.id}\", \"{conn.example}\") in the "
               "sandbox toolset. No token or login step needed.")
        route = "cli"
    elif _needs_connect(it):
        signed_in = (browser_status or {}).get(it.id) == "signed_in"
        after = (f"the `{it.toolset}` toolset, git, git_push and gh work" if conn and conn.secret
                 else f"use {it.tool}(…)" if it.tool else f"use cli(\"{it.id}\", …)")
        rec = (f"{it.name} isn't connected to Todd yet" + (", but the browser is signed in to it. " if signed_in else ". ")
               + f"Call cli_login(\"{it.id}\"): Todd signs the {conn.name if conn else 'CLI'} in with "
               f"{'that' if signed_in else 'the browser'} session and approves it itself (the human only steps in "
               f"for a password/2FA page or a final click the site keeps for people). Afterwards {after}. "
               + ("" if signed_in else f"If the browser isn't signed in to {it.name}, request_signins first. ")
               + "Don't create tokens or keys in the browser.")
        route = "connect"
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
    connectable = [it.id for it in map(_find, services) if it and _needs_connect(it)]
    status: dict[str, str] = {}
    if connectable:
        from . import accounts

        try:
            st = await accounts.statuses(connectable)
            status = {a["id"]: a["status"] for a in st["accounts"]}
        except Exception:  # noqa: BLE001  (browser offline: the recommendation still works)
            pass
    return {"services": [assess(s, available, status) for s in services]}


def setup_pending(ids: list[str]) -> list[Integration]:
    """Integrations among these service ids whose keys Todd still has to create in the browser."""
    out = []
    for sid in ids:
        it = _find(sid)
        if it and it.id == sid and it.setup and not all(vault.has_secret(s) for s in it.api_secrets):
            out.append(it)
    return out
