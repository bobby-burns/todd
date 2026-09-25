<div align="center">

# Todd

**A self-hosted, bring-your-own-model multi-agent operator.**

Describe an outcome. Todd designs the agents, gives them the right tools, runs them in parallel,
and asks you before it spends money or posts anything.

[![CI](https://github.com/bobby-burns/todd/actions/workflows/ci.yml/badge.svg)](https://github.com/bobby-burns/todd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Next.js 16](https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs&logoColor=white)
![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)

[Quick start](#quick-start) · [Features](#features) · [Architecture](#architecture) ·
[Extending](#extending-todd) · [Contributing](#contributing) · [Security](#security)

</div>

---

Give Todd a goal like *"launch a waitlist site for dropin.hockey with Firebase auth"*. Its planner designs and
spawns the agents the job needs and gives each one the right toolsets: a code sandbox, a real signed-in
browser, infra APIs, your plugins and MCP servers. The agents run in parallel, and Todd pauses for you before
spending money or posting anything. Every agent gets its own live window in the dashboard, thinking included.

Open source and self-hosted: `docker compose up` and it's yours.

> [!WARNING]
> Todd has **no dashboard login yet**. It binds to `127.0.0.1` by default. Don't expose it to the internet.
> See [Status and known limits](#status-and-known-limits).

## Table of contents

- [Features](#features)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Dashboard](#dashboard)
- [Architecture](#architecture)
- [Extending Todd](#extending-todd)
- [Development](#development)
- [Status and known limits](#status-and-known-limits)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## Features

- **Dynamic agents, grouped sensibly.** There's no hardcoded team. The planner calls
  `spawn_agent(name, instructions, task, tasks, toolsets, model)` for each *group of related work* ("Store
  Listing" = keywords + metadata + submit; "Marketing Site & Domain" = domain + banner + deploy), not one
  agent per step. Independent groups run in parallel (typically 1–4 agents; limits are configurable), and the
  planner waits on, messages or cancels them.
- **Watch them think, pause them, steer them.** Each agent has its own window with its reasoning (narrated,
  plus native model reasoning where supported), tool calls, browser steps, a **Pause/Resume** button and a
  message box. Pause an agent (or the whole run), tell it what to change, resume. Pausing also freezes an
  agent's in-progress browser task.
- **Every agent ends with a summary.** Done / Outputs / How / Left-needs-you, shown as a card in its window.
  The planner's run summary adds a line per agent. If an agent is stopped or fails, Todd writes a fallback
  summary from what it actually did.
- **Sign in once.** The setup wizard and the **Accounts** page cover about 70 services (dev and deploy,
  domains, cloud, payments, Google, socials, launch communities, productivity, app stores, AI platforms). Todd
  detects logins in the agents' browser and shows each session's status. The planner checks the accounts it
  needs before starting, so agents don't stall on login screens.
- **Your Claude plan, or your own keys.** By default every agent (planner included) runs as a headless
  [Claude Code](https://code.claude.com) session signed in with your Claude account, so usage counts toward
  your Pro/Max plan instead of API credits (Opus 5.5 by default). Or switch Settings → Engine to **API keys**
  and use any LiteLLM model per tier: Anthropic, OpenAI, Gemini, OpenRouter, Groq, DeepSeek, or fully local via
  Ollama.
- **APIs and MCP servers first, browser last.** Every agent calls `find_integrations` before touching a
  service. It picks, in order: a ready toolset (Vercel, GitHub, your plugins, `mcp_*` servers), the service's
  REST API via `api_request` with a vault key, a CLI in the sandbox, and only then the browser. `browse`
  requires a `why_not_api` reason, which is shown in the agent's window. Settings suggests official MCP
  servers (GitHub, Vercel, Stripe, Supabase, Neon, Linear, Notion, Sentry, Figma) with one-click add.
- **Spending rules the model can't override.** An auto-approve limit, per-run budgets and approval prompts,
  with every charge in a ledger. Card payments always need your approval. Card details reach the browser only
  as masked placeholders, only on the approved merchant's exact domains, with screenshots turned off.
- **Public actions need approval.** Agents must show you the exact text before posting, messaging or
  publishing from your accounts.
- **Extensible.** Each plugin file in `./plugins` becomes a toolset, each MCP server becomes a toolset, and
  every system and harness prompt can be edited.
- **Survives restarts.** LangGraph + a Postgres checkpointer save every run, so a crashed run shows a
  **Resume** button.

## Quick start

**Requirements:** Docker with Compose v2.24+. Optionally a Claude Pro/Max plan, or API keys for any
LiteLLM-supported provider.

```bash
git clone https://github.com/bobby-burns/todd.git
cd todd
cp .env.example .env            # optional: change SANDBOX_TOKEN / POSTGRES_PASSWORD
docker compose up -d --build    # the first build takes a few minutes
open http://localhost:3000

# sign in to Claude once (Anthropic's own sign-in; pick your Claude subscription)
docker compose exec -it api claude auth login
```

The **setup wizard** (Setup in the sidebar) walks you through the rest:

1. **Engine:** "Your Claude plan" (shows whether Claude Code is signed in, with a Test button) or "API keys"
   (pick a provider, paste its key, test).
2. **Integrations:** a Vercel token and a GitHub token (fine-grained, repo administration + contents). Install
   the Vercel GitHub app on your account so new repos can be connected.
3. **Spending:** auto-approve limit, default run budget, domain registrant contact.
4. **Sign in to accounts:** tick the services your agents should use. Todd opens each login page in the
   agents' browser (embedded next to the list). You sign in, Todd detects it and moves to the next.

Manage sessions any time on **Accounts**: status and how it was detected (session cookie, verified by visit,
via Google…), sign in, verify, sign out, or add custom sites. A payment card for browser checkouts (use a
virtual card with a limit) goes in Settings.

### Fully local models

```bash
docker compose --profile local up -d
docker compose exec ollama ollama pull qwen3:14b
```

Then set a role to `ollama_chat/qwen3:14b` in Settings.

## Configuration

Everything in [`.env.example`](.env.example) is optional. Keys can also be added in the dashboard
(Settings → Models / Integrations), where they're stored encrypted in Postgres and take precedence over
environment variables.

| Variable                     | Purpose                                                                  |
|------------------------------|--------------------------------------------------------------------------|
| `POSTGRES_PASSWORD`          | Database password                                                        |
| `SANDBOX_TOKEN`              | Shared secret between the API and the sandbox. **Change it.**            |
| `TODD_SECRET_KEY`            | Fernet key for the vault (auto-generated into the data volume if empty)  |
| `VNC_PASSWORD`               | Password for the live browser view (noVNC)                               |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | Model keys for the **API keys** engine (never passed to Claude Code) |
| `VERCEL_TOKEN`, `GITHUB_TOKEN` | Integration tokens (fallbacks for the vault)                           |
| `TODD_PLANNER_MAX_TURNS`, `TODD_CODE_MAX_STEPS`, `TODD_BROWSER_MAX_STEPS` | Step limits                  |

## Dashboard

A glass-material interface (light, dark or follow-system) built with Next.js, Tailwind v4 and Motion. There's
one window per agent with live reasoning, paired tool calls (spinner → check), browser steps with screenshots,
chat-style messages, pause/resume and end-of-work summary cards. Approval and spend requests appear as sheets at
the top of the run. It works on phones too, with a floating tab bar.

The design system lives in `services/web/app/globals.css` (color tokens + the `glass` material),
`components/ui.tsx` (status pills, segmented controls, switches, avatars) and `lib/motion.ts` (spring presets).

## Architecture

```
web (Next.js :3000) ──proxy/SSE + token──► api (FastAPI, internal)
                                     ├─ planner (LangGraph + Postgres checkpointer)
                                     │    find_integrations / check_accounts / request_signins
                                     │    spawn_agent(tasks=[…]) / wait_for_agents / message_agent / cancel_agent
                                     │      │
                                     │      ├─► agent "Store Listing"            toolsets: web, browser, accounts
                                     │      ├─► agent "Marketing Site & Domain"  toolsets: sandbox, vercel, porkbun
                                     │      └─► agent "Launch Assets"            toolsets: sandbox, web   (1–4 typical)
                                     │
                                     │  toolsets: sandbox ─► sandbox container (node/git/python)
                                     │            browser ─► browser-use ─CDP─► Chromium + noVNC :6080
                                     │            vercel, github, web, vault, accounts, plugins, mcp_*
                                     └─ postgres: runs, agents, events, approvals, vault, ledger, checkpoints
```

| Service    | Role                                                                        | Exposed          |
|------------|-----------------------------------------------------------------------------|------------------|
| `web`      | Next.js dashboard; proxies `/api/*` (including SSE) to the API              | `127.0.0.1:3000` |
| `api`      | FastAPI + LangGraph orchestrator, tools, vault, spend policy                | internal (token) |
| `postgres` | Runs, events, approvals, vault, ledger, settings, checkpoints               | internal         |
| `sandbox`  | Node 22 / pnpm / git / Python / deploy CLIs, with a small exec API          | internal         |
| `browser`  | Headful Chromium on Xvfb; CDP + noVNC live view                             | `127.0.0.1:6080` |
| `ollama`   | Optional (`--profile local`) for local models                               | internal         |

- **Parallelism:** background agents run at the same time (max 4 at once, 12 per run by default). Browser
  tasks share one browser and queue.
- **Pause:** each agent (and the planner) has a pause gate checked before every model call and tool call. A
  paused browser task is paused inside browser-use. Messages sent while paused are read when it resumes.
- **Human in the loop:** `ask_human`, `request_approval` and the spend policy pause *in place* (no
  `interrupt()` re-execution), so a 10-minute browser task isn't restarted when you answer.
- **Secrets:** stored encrypted, referenced by tools as `{{secret:NAME}}`, never placed in model context.
- **Isolation:** the sandbox (agent-run code) and the browser (untrusted pages) each share a network only with
  the API, and every API call needs a token that only `api` and `web` hold.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the module map, the lifecycle of a run, both engines and the full
security model.

## Extending Todd

### Custom tools (toolsets)

```python
# plugins/slack.py
from todd.sdk import todd_tool, get_secret

@todd_tool(toolset="slack")             # agents spawned with toolsets=["slack"] get it
async def slack_post(channel: str, text: str) -> str:
    """Post a message to Slack.

    Args:
        channel: e.g. #launches
        text: message
    """
    ...
```

Each plugin file becomes a toolset (named after the file unless you set `toolset=`), and the planner sees
every toolset's description when it designs agents. `planner=True` also gives a tool to the planner directly.
Reload from **Settings → Toolsets & plugins**. Plugins get `get_ctx()` (events, `ask_human`,
`request_approval`), `get_agent_id()`, `authorize_spend()` for anything that costs money, and vault access. Any
existing LangChain tool works too. See [`plugins/example_notify.py`](plugins/example_notify.py).

### MCP servers

MCP servers are configured in Settings as JSON (langchain-mcp-adapters format). Each one becomes a toolset
named `mcp_<name>`.

### Prompts

Every prompt is a markdown file: `harness` (prepended to all agents), `planner`, `worker` (the template for
spawned agents; the planner's instructions are filled in) and `browser`. Override them in
`./prompts/<name>.md` or in the dashboard. See [`prompts/README.md`](prompts/README.md).

## Development

```bash
# API tests (scripted fake models, no keys needed). With the sandbox server running they also cover spawned
# agents using the sandbox; with a browser reachable over CDP (BROWSER_CDP_HOST) they also test Accounts.
cd services/api && pip install -r requirements.txt pytest
TODD_DATA_DIR=$(mktemp -d) TODD_PLUGINS_DIR=tests/plugins pytest -q tests

# inside compose
docker compose exec api sh -c 'pip install pytest && TODD_DATA_DIR=$(mktemp -d) DATABASE_URL= TODD_API_TOKEN_FILE= TODD_PLUGINS_DIR=tests/plugins SANDBOX_WORKSPACE=/workspace pytest -q tests'
docker compose exec api python tests/browser_smoke.py      # drives the real browser container
```

Dashboard: `cd services/web && npm install && API_URL=http://localhost:8000 npm run dev`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full setup, project layout and guidelines.

## Status and known limits

Todd is at **v0.4** and under active development.

- **No dashboard login yet.** The dashboard and live view bind to 127.0.0.1. Don't expose Todd publicly until
  auth is added. (Internally, the API requires a generated token and the sandbox and browser are
  network-isolated; see [ARCHITECTURE.md](ARCHITECTURE.md#security-model).)
- **One shared browser.** Browser tasks run one at a time; other agents keep working in parallel.
- **Login detection is best-effort.** It's exact for services with a known session cookie. For others Todd
  loads a dashboard page and looks for a login redirect or a visible password field, and you can mark a
  service yourself.
- Background agents don't survive an API restart. Resume continues the planner, which can re-spawn them.
- **Vercel ↔ GitHub:** creating a Git-connected project requires the Vercel GitHub app to have access to
  the repo.
- Screenshots are taken only for http(s) pages (a browser-use limitation).
- **Claude plan engine:** plan usage limits apply. Several Opus agents in parallel use them faster; when a limit
  is hit, the run stops with a message and you can resume it after the reset. Todd never handles your Claude
  credentials: the unmodified Claude Code CLI keeps its own sign-in in the `apidata` volume. Claude Code's own
  tools (Bash, file edits, web) are switched off; agents only get Todd's toolsets. In this engine agents drive the
  browser themselves step by step (`browser_start` … `browser_done`) instead of handing a task to browser-use's
  LLM, so every model call goes through your plan. The engine is fixed per run; switching affects new runs.
- Pause takes effect at the agent's next step: a model call or tool call already in flight finishes first.
- `api_request` only sends `GITHUB_TOKEN` / `VERCEL_TOKEN` to their own API hosts; model provider keys are
  never sent anywhere by tools, nor is the payment card. Other vault keys can go to any host the agent
  chooses, so only store keys you're comfortable with agents using.

## Roadmap

Ideas under consideration. Feedback and PRs welcome.

- Recorded browser "recipes": the first run explores, later runs replay a saved script and fall back to the
  model only when something changes
- Multiple browser instances (parallel browser agents)
- Agents that spawn sub-agents
- Saved agent templates
- Virtual-card issuing providers
- Dashboard auth and multi-user
- Per-run workspace download
- Scheduled runs
- More infra tools (Cloudflare, Supabase, Stripe)

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and
guidelines, and follow the [Code of Conduct](CODE_OF_CONDUCT.md). For larger changes, open an issue first so we
can agree on the approach.

## Security

Todd can spend money and act from your accounts. If you find a vulnerability, **please report it privately**
as described in [SECURITY.md](SECURITY.md) rather than opening a public issue.

## License

Todd is released under the [MIT License](LICENSE).
