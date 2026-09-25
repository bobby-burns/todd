"""API-first infra tools: Vercel (domains, DNS, projects, env, deploys) and GitHub (repos).

Tokens come from the vault (VERCEL_TOKEN, GITHUB_TOKEN) and never reach the model.
Values passed to tools may reference vault secrets as {{secret:NAME}}; they are resolved here.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .. import settings, vault
from ..policy import authorize_spend, settle
from ..sdk import ToolError, get_ctx, todd_tool

VERCEL_API = "https://api.vercel.com"
GITHUB_API = "https://api.github.com"
_SECRET_REF = re.compile(r"\{\{secret:([A-Za-z0-9_\-]+)\}\}")


PUBLIC_ENV_PREFIXES = ("NEXT_PUBLIC_", "VITE_", "PUBLIC_", "EXPO_PUBLIC_", "REACT_APP_", "NUXT_PUBLIC_", "GATSBY_")


def resolve_secrets(value: str, allow_protected: bool = False) -> str:
    def sub(m: re.Match) -> str:
        if not allow_protected and vault.is_protected(m.group(1)):
            raise ToolError(f"secret {m.group(1)!r} is protected (integration tokens, model keys, cards) and "
                            "can't be injected into values")
        v = vault.get_secret(m.group(1))
        if v is None:
            raise ToolError(f"secret {m.group(1)!r} is not in the vault")
        return v

    return _SECRET_REF.sub(sub, value)


# ---------------------------------------------------------------------------------------- Vercel
async def _vercel(method: str, path: str, *, json: Any = None, params_: dict | None = None) -> Any:
    token = vault.get_secret("VERCEL_TOKEN")
    if not token:
        raise ToolError("VERCEL_TOKEN is not configured. Ask the human to add it in Settings → Integrations.")
    q = dict(params_ or {})
    team = settings.get("integrations").get("vercel_team_id")
    if team:
        q["teamId"] = team
    async with httpx.AsyncClient(base_url=VERCEL_API, timeout=60) as c:
        r = await c.request(method, path, json=json, params=q, headers={"Authorization": f"Bearer {token}"})
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:2000]}
    if r.status_code >= 400:
        raise HTTPToolError(r.status_code, f"Vercel {method} {path} -> {r.status_code}: {data}")
    return data


class HTTPToolError(ToolError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        for k in ("amount", "price", "value"):
            if k in value:
                return _price(value[k])
    return None


async def _check_domain(domain: str, years: int = 1) -> dict:
    avail = await _vercel("GET", f"/v1/registrar/domains/{domain}/availability")
    out: dict[str, Any] = {"domain": domain, "available": bool(avail.get("available"))}
    if out["available"]:
        price = await _vercel("GET", f"/v1/registrar/domains/{domain}/price", params_={"years": str(years)})
        out.update(
            years=price.get("years", years),
            purchase_price_usd=_price(price.get("purchasePrice")),
            renewal_price_usd=_price(price.get("renewalPrice")),
        )
    return out


@todd_tool(toolset="vercel")
async def vercel_check_domain(domain: str, years: int = 1) -> dict:
    """Check whether a domain can be registered through Vercel, and its price.

    Args:
        domain: domain name, e.g. example.com
        years: registration length in years (default 1)
    """
    return await _check_domain(domain, years)


@todd_tool(toolset="vercel")
async def vercel_buy_domain(domain: str, expected_price_usd: float, years: int = 1, auto_renew: bool = True) -> dict:
    """Register a domain through Vercel (charged to the Vercel account's card). Goes through the spend policy and
    may pause for human approval. Always call vercel_check_domain first and pass the exact price it returned.
    Domains bought through Vercel get their DNS managed by Vercel automatically.

    Args:
        domain: domain to buy
        expected_price_usd: exact purchase price from vercel_check_domain
        years: registration length in years (default 1)
        auto_renew: renew automatically before expiry (default true)
    """
    ctx = get_ctx()
    contact = settings.get("registrant_contact")
    missing = [k for k in ("firstName", "lastName", "email", "phone", "address1", "city", "state", "zip", "country")
               if not contact.get(k)]
    if missing:
        raise ToolError("Registrant contact info is incomplete (" + ", ".join(missing) + "). Ask the human to fill "
                        "Settings → Domain registrant, then retry.")
    check = await _check_domain(domain, years)
    if not check["available"]:
        raise ToolError(f"{domain} is not available.")
    actual = check.get("purchase_price_usd")
    if actual is not None and abs(actual - expected_price_usd) > 0.01:
        raise ToolError(f"Price changed: expected ${expected_price_usd}, actual ${actual}. Retry with the new price.")
    entry = await authorize_spend(ctx, amount_usd=expected_price_usd, merchant="Vercel Domains",
                                  description=f"Register {domain} for {years} year(s)", method="account",
                                  data={"domain": domain, "years": years})
    body = {"autoRenew": auto_renew, "years": years, "expectedPrice": expected_price_usd,
            "contactInformation": {k: v for k, v in contact.items() if v}}
    try:
        order = await _vercel("POST", f"/v1/registrar/domains/{domain}/buy", json=body)
    except HTTPToolError as e:
        # 4xx: Vercel rejected the order, nothing was charged. 5xx: unknown -> a human should check.
        settle(entry, "failed" if 400 <= e.status < 500 else "needs_review", {"error": str(e)[:500]})
        raise
    except Exception as e:  # timeouts / network: the order may or may not have gone through
        settle(entry, "needs_review", {"error": f"{type(e).__name__}: {e}"[:500]})
        raise ToolError(f"Purchase outcome unknown ({type(e).__name__}). Check vercel.com/domains before retrying; "
                        "the ledger entry is marked needs_review.") from e
    settle(entry, "completed", {"order": order})
    return {"purchased": domain, "order": order}


@todd_tool(toolset="vercel")
async def vercel_create_project(name: str, github_repo: str | None = None, framework: str | None = None) -> dict:
    """Create a Vercel project, optionally connected to a GitHub repo so pushes auto-deploy.
    The Vercel GitHub app must have access to the repo.

    Args:
        name: project name (lowercase, dashes)
        github_repo: GitHub repo as owner/name
        framework: e.g. nextjs, vite, astro; omit to auto-detect
    """
    body: dict[str, Any] = {"name": name}
    if framework:
        body["framework"] = framework
    if github_repo:
        body["gitRepository"] = {"type": "github", "repo": github_repo}
    p = await _vercel("POST", "/v11/projects", json=body)
    return {"id": p.get("id"), "name": p.get("name"), "link": p.get("link"), "framework": p.get("framework")}


@todd_tool(toolset="vercel")
async def vercel_add_domain(project: str, domain: str, redirect_www: bool = True) -> dict:
    """Attach a domain to a Vercel project (and optionally redirect www to the apex).

    Args:
        project: project name or id
        domain: apex domain, e.g. example.com
        redirect_www: also add www.<domain> redirecting to the apex (default true)
    """
    res = await _vercel("POST", f"/v10/projects/{project}/domains", json={"name": domain})
    out = {"added": domain, "verified": res.get("verified"), "verification": res.get("verification")}
    if redirect_www and not domain.startswith("www."):
        try:
            await _vercel("POST", f"/v10/projects/{project}/domains",
                          json={"name": f"www.{domain}", "redirect": domain, "redirectStatusCode": 308})
            out["www_redirect"] = True
        except ToolError as e:
            out["www_redirect_error"] = str(e)
    return out


@todd_tool(toolset="vercel")
async def vercel_domain_status(domain: str) -> dict:
    """Get a domain's DNS/configuration status on Vercel (e.g. misconfigured, records needed).

    Args:
        domain: domain name
    """
    return await _vercel("GET", f"/v6/domains/{domain}/config")


@todd_tool(toolset="vercel")
async def vercel_set_env(project: str, key: str, value: str, targets: list[str] | None = None) -> dict:
    """Set (upsert) an environment variable on a Vercel project. Use {{secret:NAME}} in value to inject a vault
    secret without seeing it.

    Args:
        project: project name or id
        key: variable name, e.g. NEXT_PUBLIC_FIREBASE_API_KEY
        value: the value, or {{secret:NAME}}
        targets: any of production, preview, development (default all three)
    """
    if _SECRET_REF.search(value) and key.upper().startswith(PUBLIC_ENV_PREFIXES):
        raise ToolError(f"{key} is a public (client-bundled) variable; vault secrets can't be injected into it.")
    body = {"key": key, "value": resolve_secrets(value), "type": "encrypted",
            "target": targets or ["production", "preview", "development"]}
    await _vercel("POST", f"/v10/projects/{project}/env", json=body, params_={"upsert": "true"})
    return {"set": key, "project": project, "targets": body["target"]}


@todd_tool(toolset="vercel")
async def vercel_deploy(project: str, github_repo: str, ref: str = "main") -> dict:
    """Trigger a production deployment of a GitHub-connected project at a git ref.

    Args:
        project: project name
        github_repo: owner/name
        ref: branch or commit (default main)
    """
    org, repo = github_repo.split("/", 1)
    body = {"name": project, "project": project, "target": "production",
            "gitSource": {"type": "github", "org": org, "repo": repo, "ref": ref}}
    d = await _vercel("POST", "/v13/deployments", json=body)
    return {"id": d.get("id"), "url": d.get("url"), "readyState": d.get("readyState")}


@todd_tool(toolset="vercel")
async def vercel_deployment_status(deployment_id: str) -> dict:
    """Check a deployment's state (QUEUED/BUILDING/READY/ERROR), URL and aliases.

    Args:
        deployment_id: deployment id returned by vercel_deploy
    """
    d = await _vercel("GET", f"/v13/deployments/{deployment_id}")
    return {"id": d.get("id"), "url": d.get("url"), "readyState": d.get("readyState"),
            "errorMessage": d.get("errorMessage"), "aliases": d.get("alias")}


# ---------------------------------------------------------------------------------------- GitHub
async def _github(method: str, path: str, *, json: Any = None) -> Any:
    token = vault.get_secret("GITHUB_TOKEN")
    if not token:
        raise ToolError("GITHUB_TOKEN is not configured. Ask the human to add it in Settings → Integrations.")
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=60) as c:
        r = await c.request(method, path, json=json, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise ToolError(f"GitHub {method} {path} -> {r.status_code}: {data.get('message', data)}")
    return data


async def github_whoami() -> str:
    return (await _github("GET", "/user"))["login"]


@todd_tool(toolset="github")
async def github_create_repo(name: str, private: bool = True, description: str = "") -> dict:
    """Create a GitHub repository for the project. Returns full_name (owner/name) for other tools.

    Args:
        name: repository name
        private: create as private (default true)
        description: short description
    """
    owner = settings.get("integrations").get("github_owner")
    me = await github_whoami()
    body = {"name": name, "private": private, "description": description, "auto_init": False}
    path = "/user/repos" if not owner or owner == me else f"/orgs/{owner}/repos"
    r = await _github("POST", path, json=body)
    return {"full_name": r["full_name"], "html_url": r["html_url"], "default_branch": r.get("default_branch") or "main"}


# ---------------------------------------------------------------------------------------- Vault
@todd_tool(toolset="vault", planner=True)
async def vault_list() -> list[str]:
    """List the names of secrets in the vault (values are never shown). Reference them as {{secret:NAME}}."""
    return [x["name"] for x in vault.list_secrets()]


@todd_tool(toolset="vault")
async def vault_store(name: str, value: str) -> str:
    """Store a value (e.g. an API key the browser agent obtained) in the encrypted vault.

    Args:
        name: UPPER_SNAKE_CASE name
        value: the secret value
    """
    if not re.fullmatch(r"[A-Z0-9_]{2,64}", name):
        raise ToolError("Secret names must be UPPER_SNAKE_CASE")
    if vault.is_protected(name):
        raise ToolError("Agents may not overwrite integration tokens, model keys or card details.")
    vault.set_secret(name, value)
    return f"Stored {name}. Reference it as {{{{secret:{name}}}}} in tool arguments."


VERCEL_TOOLS = [vercel_check_domain, vercel_buy_domain, vercel_create_project, vercel_add_domain,
                vercel_domain_status, vercel_set_env, vercel_deploy, vercel_deployment_status]
GITHUB_TOOLS = [github_create_repo]
VAULT_TOOLS = [vault_list, vault_store]
