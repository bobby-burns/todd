"""Is the website a run built ready for real visitors? A checklist read from the files the agents left.

Shown on a finished run (the dashboard's Launch checklist): what's in place (a title and description, a social
preview, icons, robots.txt, sitemap.xml, llms.txt, a 404 page, security headers…) and, for what's missing, a
prompt that has Todd add it. It reads the project's files, not the live site, so it works before anything is online.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from . import workspace

WEB_DEPS = {"next": "Next.js", "astro": "Astro", "@sveltejs/kit": "SvelteKit", "nuxt": "Nuxt", "@remix-run/react":
            "Remix", "gatsby": "Gatsby", "vite": "Vite", "react-scripts": "Create React App", "vue": "Vue",
            "svelte": "Svelte", "react": "React"}

# (id, label, file patterns that satisfy it, content pattern that satisfies it, the fix, optional)
ITEMS: list[tuple[str, str, str | None, str | None, str, bool]] = [
    ("meta", "Page titles and descriptions", None,
     r"<meta[^>]+name=[\"']description|\bdescription\s*:|generateMetadata",
     "a title and description for every page", False),
    ("social", "Social preview image", r"(^|/)(opengraph-image|twitter-image|og(-image)?)\.(png|jpe?g|tsx?|jsx?)$",
     r"openGraph|og:image|twitter:image", "a social preview image (Open Graph and Twitter card)", False),
    ("icons", "Favicon and app icons", r"(^|/)(favicon\.(ico|svg|png)|(apple-)?icon\d*\.(png|svg|ico|tsx?|jsx?)|"
     r"apple-touch-icon[\w.-]*\.png)$", None, "a favicon and app icons", False),
    ("robots", "robots.txt", r"(^|/)robots\.(txt|ts|js)$", None, "robots.txt", False),
    ("sitemap", "sitemap.xml", r"(^|/)sitemap[\w.-]*\.(xml|ts|js)$", r"next-sitemap|@astrojs/sitemap",
     "sitemap.xml", False),
    ("llms", "llms.txt (a guide for AI assistants)", r"(^|/)llms(-full)?\.txt$|(^|/)llms\.txt/route\.(ts|js)$", None,
     "llms.txt (a short plain-text guide to the site for AI assistants)", False),
    ("404", "A friendly 404 page", r"(^|/)(not-found\.(tsx?|jsx?)|404\.(html|tsx?|jsx?|astro|vue|svelte)|\+error\.svelte)$",
     None, "a friendly 404 page", False),
    ("headers", "Security headers", None,
     r"Content-Security-Policy|Strict-Transport-Security|X-Content-Type-Options",
     "security headers (Content-Security-Policy, Strict-Transport-Security, X-Content-Type-Options, Referrer-Policy)",
     False),
    ("manifest", "Web app manifest", r"(^|/)(manifest\.(json|webmanifest|ts|js)|site\.webmanifest)$", None,
     "a web app manifest", True),
    ("privacy", "Privacy page", r"(^|/)privacy([-_]policy)?(/|\.|$)", None,
     "a privacy page saying what the site stores (needed if it has analytics, accounts or email sign-ups)", True),
]
# Files whose contents answer the content checks (read at most this many).
_CONTENT = re.compile(r"(^|/)((root)?layout|_document|_app|app|head|seo|meta(data)?|index|base(head|layout)?|"
                      r"next\.config|nuxt\.config|astro\.config|svelte\.config|vite\.config|middleware|proxy)"
                      r"\.(tsx?|jsx?|mjs|cjs|html|astro|vue|svelte)$|(^|/)(vercel\.json|netlify\.toml|_headers)$",
                      re.IGNORECASE)
_SKIP = re.compile(r"(^|/)(node_modules|\.next|dist|build|\.git|\.vercel|out|coverage)/")
MAX_READ = 14


async def check(run_id: str) -> dict[str, Any]:
    t = await workspace.tree(run_id)
    files = [e["path"] for e in t.get("entries", []) if not e.get("dir") and not _SKIP.search(e["path"])]
    project, framework = await _project(run_id, files)
    if project is None:
        return {"web": False, "items": []}
    base = project + "/" if project else ""
    mine = [f[len(base):] for f in files if f.startswith(base)]
    candidates = sorted((f for f in mine if _CONTENT.search(f)), key=lambda f: (f.count("/"), f))
    texts = await _read(run_id, base, candidates[:MAX_READ])
    pkg = texts.pop("package.json", "")
    items = []
    for key, label, file_re, content_re, fix, optional in ITEMS:
        found = next((f for f in mine if file_re and re.search(file_re, f, re.IGNORECASE)), None)
        where = found
        if not found and content_re:
            hit = next((n for n, txt in texts.items() if re.search(content_re, txt)), None)
            if hit is None and re.search(content_re, pkg):
                hit = "package.json"
            where = hit
        items.append({"id": key, "label": label, "ok": bool(where), "where": where, "fix": fix, "optional": optional})
    missing = [i for i in items if not i["ok"] and not i["optional"]]
    prompt = ""
    if missing:
        prompt = ("Make the site ready for real visitors: add " + _join([i["fix"] for i in missing]) +
                  ". Keep what's there, check everything on localhost, then put it back online the way it is now "
                  "(ask me before a production deploy) and check each one loads on the live address.")
    return {"web": True, "project": project or ".", "framework": framework, "items": items, "missing": len(missing),
            "prompt": prompt}


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


async def _project(run_id: str, files: list[str]) -> tuple[str | None, str]:
    """The web project's folder in the run (the shallowest one) and its framework."""
    pkgs = sorted((f for f in files if posixpath.basename(f) == "package.json"), key=lambda f: f.count("/"))
    for p in pkgs[:6]:
        try:
            data = json.loads((await workspace.view(run_id, p)).get("content") or "{}")
        except Exception:  # noqa: BLE001
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        for dep, name in WEB_DEPS.items():
            if dep in deps:
                return posixpath.dirname(p), name
    html = sorted((f for f in files if posixpath.basename(f) == "index.html"), key=lambda f: f.count("/"))
    if html:
        return posixpath.dirname(html[0]), "static site"
    return None, ""


async def _read(run_id: str, base: str, rel: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in ["package.json", *rel]:
        try:
            r = await workspace.view(run_id, base + f)
        except Exception:  # noqa: BLE001
            continue
        if r.get("kind") == "text":
            out[f] = (r.get("content") or "")[:200_000]
    return out
