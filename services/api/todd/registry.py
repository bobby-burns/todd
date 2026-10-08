"""Toolsets: the building blocks the planner hands to the agents it spawns.

  * built-in toolsets (sandbox, browser, vercel, github, web, video, shorts, trends, vault, accounts)
  * plugin toolsets: every LangChain tool found in ./plugins/*.py (module-level BaseTool instances or a `TOOLS`
    list). A tool's toolset is `todd_tool(toolset=...)`, defaulting to the plugin file's name.
  * MCP toolsets: one per MCP server configured in Settings ("mcp_<server>").

The planner itself only orchestrates: it gets the orchestration tools, human tools, and any tool marked
`planner=True` (check_accounts, fetch_url, vault_list, …).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from . import settings
from .accounts import ACCOUNT_TOOLS
from .agents.dynamic import ORCHESTRATION_TOOLS
from .integrations import find_integrations
from .sdk import SOURCE_KEY, for_planner, tag, toolset_of
from .tools.browser_tools import BROWSER_TOOLS
from .tools.cli_login import CLI_LOGIN_TOOLS
from .tools.human import HUMAN_TOOLS
from .tools.infra import GITHUB_TOOLS, VAULT_TOOLS, VERCEL_TOOLS, resolve_secrets
from .tools.sandbox_tools import SANDBOX_TOOLS
from .tools.critic import CRITIC_TOOLS
from .tools.shorts import SHORTS_TOOLS
from .tools.trends import TRENDS_TOOLS
from .tools.video import VIDEO_TOOLS
from .tools.web import WEB_TOOLS

log = logging.getLogger("todd.registry")
PLUGINS_DIR = Path(os.getenv("TODD_PLUGINS_DIR", "/app/custom/plugins"))


@dataclass
class Toolset:
    name: str
    description: str
    tools: list[BaseTool] = field(default_factory=list)
    source: str = "builtin"
    guide: str = ""  # usage tips added to the system prompt of agents that get this toolset
    hidden: bool = False  # used by tools themselves (e.g. the critic short_critique starts), not offered to the planner

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "source": self.source,
                "tools": [t.name for t in self.tools]}


# How a short gets made, for agents with the shorts toolset. A format seen working in the niche comes first, then a
# shot list from it, then footage for each shot; the look decides whether anyone believes a person made it.
SHORTS_GUIDE = """\
The look: it has to pass for something a person filmed on their phone and cut in an editing app, never a template.
- Real footage, full-frame: the product's own screens (short_record reads as a phone screen recording), phone-style
  b-roll (short_stock: plain, handheld-looking clips of places, hands, things), and AI shots for people (prompted
  as phone video, see below). Never design frames: no made-up backgrounds, gradients, slides, cards, mockups, logos,
  badges, countdowns or end cards, no URLs or calls to action on screen (the link goes in the post's caption), and
  never a page, image or animation built just for the video.
- Text is the app's own (short_new text_style "box" or "outline"): lowercase or sentence case, 1–2 short lines,
  up to ~8 words a line. The hook's text is on screen from the first frame. One audio mode per video:
  * text + sound: nobody talks; the on-screen text tells the story beat by beat (silent beats with `seconds`), no
    captions; it renders silent and the sound (the format's trending sound) is added in the app when posting:
    say which in `sound`;
  * voiceover: one voice over the footage, first person, how people in the niche talk (contractions, no ad words,
    the product named at most twice); phrase captions carry the words, so on-screen text is the hook's line (and at
    most a label or two), not a box on every beat; "karaoke" captions only if the format really has them;
  * natural: the footage's own sound (rare for us: screen recordings are silent).
- The edit: hard cuts, no transitions. Most shots 1.5–3 s, none under 0.5 s; something changes at least every
  2–3 s (a cut, a punch-in, new text). Use quick cuts inside a beat (`shots`), punch-ins (`focus` 1.2–1.6 centred
  on the thing being tapped or read, so none of it is cut off at the edge: a tap's mark has its x and y; often at the
  moment it happens: `at_s`), cut loading and waiting out of recordings
  (start each cut where something happens; `speed: "fit"` for a long scroll). The payoff is on screen by ~3 s;
  the last frame should loop back into the first.
- Length: as short as the idea allows. Most land at 8–20 s; 30 s is a ceiling, not a target, and a real example's
  length isn't one to match. When you make several, make at least one under 12 s.
- Honest: never present a stock or AI person as a customer or reviewer, never fake a comment, post, review,
  message or number. Film real public pages (the product, or the real place a problem lives) signed out.
From a format card to a shot list: follow the card's shots, audio, text style and pace (its `measured` avg_shot_s
is the examples'), fitting the product in where `adapt` says. Make each of the card's shots with what fits it:
  screen -> short_record of the product (or of the real public page the problem lives on), one moment per cut;
  screenshot -> a still of a real page (a recording held on it), never a made-up post, comment or chat;
  talking-head / person / pov-hands -> an AI shot (prompt below), silent with the line as on-screen text; or a stock
    clip of someone doing the thing (not talking, not a fake customer); or drop the face: text over the screen;
  place / object -> short_stock first (pick the one that looks phone-shot), an AI shot second;
  text-only -> the text over a real shot (a screen, a stock clip), never a designed card;
  reused (TV, games, other creators) -> never reuse it: a person, place or text shot that does the same job.
AI shots stay priced placeholders until the human approves the scaffold. Prompt them as phone video, one shot,
one action, 2–4 s used: "Vertical 9:16 smartphone video, <front-camera selfie at arm's length | rear camera held in
one hand | phone propped on a counter>, slightly off-center, one continuous take. <the same cast description in
every shot: age, hair, clothes, one detail>. <one simple action, small natural motion>. In <an ordinary lived-in
place>, <time of day>, lit by <a named light source>; phone auto-exposure, natural skin texture, slight handheld
shake. Screens, signs and labels in view are blank or turned away." Never ask for text, UI, logos or a speaking
mouth; never words like cinematic, epic, professional, studio.
Steps:
0) Read the format card(s) (format_search) and decide which one this short follows. Write its shot list first: for
   each of the card's shots, what our version shows and where it comes from (above). Then get exactly that footage:
   short_look a page (a screenshot and an outline with the exact texts to tap), then short_record the moments the
   shot list needs, a few seconds each (the live site, or for a repository a local copy in the sandbox at
   http://localhost:3000; start_at opens on the right section; recordings are signed out, so go through the flow
   like a visitor). short_stock / short_stock_pick for b-roll. Every recording and clip comes back with a contact
   sheet: look at it. Never write your own recorder or call ffmpeg: Todd's media service renders everything.
1) short_new with format_id, the audio mode the card has, 2–3 hook variants (the first line or text decides whether
   anyone keeps watching) and the beats. Never a feature tour; the product is the payoff, not the pitch.
2) short_voiceover (free local voice) if anything is spoken, short_plan (fix every warning, and look at vs_format:
   cut more if it's slower than the examples), then short_render the animatic and look at its contact sheet against
   the look above and the card's examples (and the card's `lessons`, from earlier critiques). Fix what doesn't pass.
3) short_critique: a separate critic watches the cut (a frame every half second with what's said and written then,
   what the soundtrack really says, frozen picture and silence) next to the card's real examples, and returns scores
   and fixes. Apply the fixes (voice again if a line changed, plan), then short_critique again. It's free; it stops
   by itself (at most 3 rounds, never after a pass, and as soon as the score stops improving). Follow its `next`.
4) short_review: the human watches the free scaffold and approves or asks for changes; apply them and review again
   until they approve. Only then: short_voiceover(provider="elevenlabs") for a voiced short, plan again, and (later)
   generate. Never post."""

TRENDS_GUIDE = """\
1) format_search first: the library may already know this niche (and cards older than ~30 days are worth refreshing).
2) Map the niche from the product: who it's for, the situations they post about (not the product), the phrases
   they'd type into TikTok search, and the hashtags they really use.
3) trend_scan with 3–6 search phrases (they find the niche better than hashtags) plus a few hashtags. Read the
   captions: a phrase or tag can mean something else (another sport, another country). If the best on-niche videos
   are few, trend_scan(related=[their ids]) once for more like them.
4) trend_analyze the 3–5 strongest on-niche videos (high reach and share rate; skip ads, big accounts and
   off-niche results). Look at every sheet: you're seeing the edit, a frame from every shot. First write down what
   is there (what each shot shows, who's on camera, how it's filmed, the exact on-screen text and where it sits,
   what's said, the cuts per second), then what it means.
5) format_save one card per format (merge videos that share one): the shot list as it really is, the audio mode,
   the text style, why it works (pointing at shots), and how a product fits without becoming an ad (`adapt`).
   Different formats matter more than more of the same: aim for cards that differ in audio (text + sound vs voice)
   and in what's on camera, including at least one that the product's own screens can carry. Never copy a video:
   learn its shape. Report each card: its id, name, audio, what it needs on camera, and its measured pace."""

BUILTIN_TOOLSETS: dict[str, Toolset] = {
    "sandbox": Toolset("sandbox", "Linux sandbox (node 22, pnpm, git, gh, python, vercel/firebase/eas CLIs) with a "
                       "workspace shared by all agents in this run: shell, read/write/list files, git and git_push (signed in to GitHub), the "
                       "GitHub CLI (`gh`) and `cli` for Vercel/Netlify/Railway/Cloudflare/Stripe/Firebase, all "
                       "signed in with the human's account (connect with cli_login), and `eas` for iPhone/Android "
                       "apps with Expo (cloud builds, TestFlight/Play uploads; connect with cli_login(\"expo\")).",
                       SANDBOX_TOOLS,
                       guide="Commands have no TTY: always pass non-interactive flags (e.g. `npx create-next-app@latest "
                             "app --ts --tailwind --eslint --app --use-npm --yes`). Verify with a real build before "
                             "pushing. Config goes in env vars with a `.env.example`; never commit `.env*`. Paths are "
                             "relative to the run workspace, which other agents may also use: stay in your directory."),
    "browser": Toolset("browser", "A real Chromium where the human is signed in to their accounts: browse(task) "
                       "runs multi-step web tasks (consoles without APIs, forms, sign-in-gated pages, card "
                       "checkout with approval, checking websites you built). One browser, shared: browser tasks run "
                       "one at a time.",
                       BROWSER_TOOLS,
                       guide="Last resort: call find_integrations first and pass why_not_api. Each browse() call should be one focused goal with a clear done condition and the exact "
                             "values to report. Don't create new accounts; the human is usually signed in already. "
                             "Copy config values (IDs, URLs) exactly into your summary, but never credentials: don't "
                             "create API keys or tokens unless the task says so, and connect services with cli_login."),
    "vercel": Toolset("vercel", "Vercel API: check/buy domains (spend-gated), projects, domains/DNS, env vars, "
                      "deployments.", VERCEL_TOOLS),
    "github": Toolset("github", "GitHub API: create repositories.", GITHUB_TOOLS),
    "web": Toolset("web", "api_request: call any REST/GraphQL API with vault secrets as {{secret:NAME}}; fetch_url: "
                   "read docs and public pages. The default way to work with services that have an API.", WEB_TOOLS,
                   guide="Look up the right API with find_integrations, read its docs with fetch_url, then call it with "
                         "api_request. Check status codes and report IDs/URLs from responses."),
    "video": Toolset("video", "Short vertical slideshow videos (9:16) from stock photos (Pixabay, Pexels) and the "
                     "run's own images: storyboard, search each shot by meaning, pick, render captioned 1080×1920 "
                     "slides and an MP4 into the run folder.", VIDEO_TOOLS,
                     guide="Write the storyboard first (video_new): 3–8 shots, one idea per slide, captions under 12 "
                           "words. For each shot call video_find_shots and pick with video_pick. Scores already favour "
                           "images that match earlier picks, so pick in order. You can't see the images: go by score "
                           "and alt text, and search again with a more concrete query when the top ones don't fit. Put "
                           "the product's own screenshots in video/library/ (or pass workspace_paths) and use them for "
                           "at least one shot. Render once with video_render and report the MP4 path (the slides are "
                           "also a TikTok photo post). Add music only if the human gave you a file for it. Don't post "
                           "anything: posting is a separate, approved step."),
    "shorts": Toolset("shorts", "Short vertical videos (TikTok, Reels, Shorts) that look made on a phone: film the "
                      "product in the agents' browser, find phone-style stock b-roll, write the script from a format "
                      "card (voiced, or text + sound), voice it (free local voice for the scaffold, ElevenLabs for "
                      "the final), plan every cut, caption and punch-in, and render a free scaffold to approve before "
                      "anything is spent.", SHORTS_TOOLS,
                      guide=SHORTS_GUIDE),
    "trends": Toolset("trends", "What's working right now in a niche: recent TikToks for what its people search and "
                      "the hashtags they use, ranked by how far they outperformed their creator's audience, decoded "
                      "shot by shot (cuts, a frame from every shot, the words over each), and a shared library of "
                      "format cards (shot list, audio, text style, measured pace, why it works) to build shorts "
                      "from.", TRENDS_TOOLS,
                      guide=TRENDS_GUIDE),
    "critic": Toolset("critic", "Watch a rendered short and give a verdict (the critic agent short_critique starts).",
                      CRITIC_TOOLS, hidden=True),
    "vault": Toolset("vault", "List secret names and store new secrets (referenced as {{secret:NAME}}).",
                     VAULT_TOOLS),
    "accounts": Toolset("accounts", "See which services the browser is signed in to; ask the human to sign in.",
                        ACCOUNT_TOOLS),
}
# Under the Claude Code engine the agent drives the browser itself (every model call goes through the human's plan).
from .tools.browser_direct import DIRECT_BROWSER_TOOLS  # noqa: E402

DIRECT_BROWSER_TOOLSET = Toolset(
    "browser", "A real Chromium where the human is signed in to their accounts, driven step by step: browser_start, "
    "then browser_navigate/click/type/keys/scroll/search/read_text/console, browser_done. For consoles without APIs, "
    "forms, sign-in-gated pages and card checkout with approval, and for checking and debugging websites you built. "
    "One browser, shared: agents take turns.",
    DIRECT_BROWSER_TOOLS,
    guide="For work on a service it's the last resort: call find_integrations first and pass why_not_api to "
          "browser_start. To check a site you built or deployed, just open it: look at the screenshot, click through, "
          "and run browser_console (reload=true catches load errors). Read the page state "
          "after each action and use element [index] numbers. Don't create new accounts; the human is usually signed "
          "in already. On captchas/2FA, ask_human (they can take over the live browser). Purchases need "
          "authorize_purchase first, and posting/messaging sites request_approval(sites=...): Todd holds those clicks "
          "back until the human approves. Always call browser_done "
          "when finished. Copy exact text (code, IDs, URLs) with browser_read_text. Credentials never pass through you: "
          "connect services with cli_login, and if the task needs a key the page shows, browser_save_secret it "
          "(browser_save_download for a key file).")

for _ts in [*BUILTIN_TOOLSETS.values(), DIRECT_BROWSER_TOOLSET]:
    for _t in _ts.tools:
        tag(_t, source="builtin")
for _t in [*ORCHESTRATION_TOOLS, *HUMAN_TOOLS, find_integrations, *CLI_LOGIN_TOOLS]:
    tag(_t, source="builtin", planner=True)

_plugin_toolsets: dict[str, Toolset] = {}
_plugin_errors: list[dict[str, str]] = []


def _builtin_tool_names() -> set[str]:
    names = {t.name for ts in [*BUILTIN_TOOLSETS.values(), DIRECT_BROWSER_TOOLSET] for t in ts.tools}
    return names | {t.name for t in [*ORCHESTRATION_TOOLS, *HUMAN_TOOLS, find_integrations, *CLI_LOGIN_TOOLS]} | {"finish"}


def load_plugins() -> None:
    """(Re)load every plugin file. Safe to call at runtime from the dashboard."""
    global _plugin_toolsets, _plugin_errors
    toolsets: dict[str, Toolset] = {}
    errors: list[dict[str, str]] = []
    reserved = _builtin_tool_names()
    if PLUGINS_DIR.is_dir():
        for path in sorted(PLUGINS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            mod_name = f"todd_plugin_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                found = list(getattr(mod, "TOOLS", []) or [])
                found += [v for v in vars(mod).values() if isinstance(v, BaseTool) and v not in found]
                desc = (mod.__doc__ or "").strip().split("\n")[0] or f"Tools from plugins/{path.name}"
                for t in found:
                    tag(t, source=f"plugin:{path.name}")
                    if t.name in reserved:
                        errors.append({"file": path.name, "error": f"tool {t.name!r} clashes with a built-in; skipped"})
                        continue
                    ts_name = toolset_of(t) or path.stem
                    if ts_name in BUILTIN_TOOLSETS:
                        errors.append({"file": path.name,
                                       "error": f"toolset {ts_name!r} is built-in; use another name ({t.name} skipped)"})
                        continue
                    tag(t, toolset=ts_name)
                    ts = toolsets.setdefault(ts_name, Toolset(ts_name, desc, [], f"plugin:{path.name}"))
                    ts.tools.append(t)
            except Exception:
                errors.append({"file": path.name, "error": traceback.format_exc(limit=3)})
                log.exception("failed to load plugin %s", path)
    _plugin_toolsets, _plugin_errors = toolsets, errors


async def load_mcp_toolsets() -> tuple[dict[str, Toolset], list[dict[str, str]]]:
    """One toolset per MCP server configured in Settings (langchain-mcp-adapters). Returns (toolsets, errors)."""
    servers: dict[str, Any] = settings.get("mcp_servers") or {}
    if not servers:
        return {}, []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    out: dict[str, Toolset] = {}
    errors: list[dict[str, str]] = []
    for name, conn in servers.items():
        try:
            desc = conn.get("description") if isinstance(conn, dict) else None
            client = MultiServerMCPClient({name: _resolve(conn)}, tool_name_prefix=True)
            tools = await client.get_tools()
            ts_name = f"mcp_{name}"
            for t in tools:
                tag(t, toolset=ts_name, source=f"mcp:{name}")
            out[ts_name] = Toolset(ts_name, desc or f"MCP server '{name}': {', '.join(t.name for t in tools[:12])}",
                                   list(tools), f"mcp:{name}")
        except Exception as e:  # noqa: BLE001
            errors.append({"server": name, "error": _root_cause(e)})
    return out, errors


def _root_cause(e: BaseException) -> str:
    """MCP clients raise ExceptionGroups from their task groups; report the innermost real error."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    msg = str(e) or type(e).__name__
    return f"{type(e).__name__}: {msg}"[:300]


def _resolve(value: Any) -> Any:
    if isinstance(value, str):
        return resolve_secrets(value, allow_protected=True)  # MCP config is written by the human
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items() if k not in ("agents", "description")}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


def all_toolsets(extra: dict[str, Toolset] | None = None, engine: str = "api") -> dict[str, Toolset]:
    builtin = dict(BUILTIN_TOOLSETS)
    if engine == "claude_code":
        builtin["browser"] = DIRECT_BROWSER_TOOLSET
    return {**builtin, **_plugin_toolsets, **(extra or {})}


def planner_tools(toolsets: dict[str, Toolset]) -> list[BaseTool]:
    out, seen = [], set()
    for t in [*ORCHESTRATION_TOOLS, *HUMAN_TOOLS, find_integrations, *CLI_LOGIN_TOOLS,
              *(t for ts in toolsets.values() for t in ts.tools if for_planner(t))]:
        if t.name not in seen:
            out.append(t)
            seen.add(t.name)
    return out


def toolsets_prompt(toolsets: dict[str, Toolset]) -> str:
    return "\n".join(f"- `{ts.name}` — {ts.description}" for ts in toolsets.values() if not ts.hidden)


def catalog(extra: dict[str, Toolset] | None = None) -> dict[str, Any]:
    from . import settings

    tsets = all_toolsets(extra, engine=settings.get("engine") or "claude_code")
    tools = []
    for t in planner_tools(tsets)[: len(ORCHESTRATION_TOOLS) + len(HUMAN_TOOLS) + 1]:
        tools.append({"name": t.name, "description": (t.description or "").split("\n")[0][:300],
                      "toolset": "planner", "planner": True, "source": "builtin"})
    for ts in tsets.values():
        for t in ts.tools:
            tools.append({"name": t.name, "description": (t.description or "").split("\n")[0][:300],
                          "toolset": ts.name, "planner": for_planner(t),
                          "source": (t.metadata or {}).get(SOURCE_KEY, ts.source)})
    return {"toolsets": [ts.info() for ts in tsets.values()], "tools": tools, "plugin_errors": _plugin_errors,
            "plugins_dir": str(PLUGINS_DIR)}
