# Todd architecture (v0.4)

## Services (docker compose)

| Service    | What it is                                                                 | Exposed            |
|------------|----------------------------------------------------------------------------|--------------------|
| `web`      | Next.js 16 dashboard. Proxies `/api/*` to the API, including SSE streams    | 127.0.0.1:3000     |
| `api`      | FastAPI + LangGraph orchestrator, tools, vault, spend policy                | internal (token)   |
| `postgres` | Runs, events, approvals, vault, ledger, settings, LangGraph checkpoints     | internal           |
| `sandbox`  | node 22 / pnpm / git / python / vercel + firebase CLIs, with a small exec API | internal         |
| `browser`  | Headful Chromium on Xvfb; CDP (via socat :9223) + noVNC live view           | 127.0.0.1:6080     |
| `ollama`   | Optional (`--profile local`) for local models                               | internal           |

## Code map (`services/api/todd`)

```
main.py            HTTP API (runs, agents, SSE events, interactions, accounts, onboarding, settings, secrets,
                   prompts, toolsets, ledger)
orchestrator.py    RunManager: per run, loads toolsets (built-in + plugins + MCP), builds the planner graph,
                   checkpointer, start/resume/cancel, messages to the planner or any agent
agents/graph.py    The LangGraph agent used by the planner and every spawned agent: model → tools → model;
                   emits thinking / thoughts / messages; finish() ends it
agents/dynamic.py  spawn_agent / wait_for_agents / message_agent / cancel_agent / list_agents; AgentHandle
                   (spawns run in the background; waits end early when a message arrives for the waiter)
agents/claude_code.py  Claude Code engine: headless `claude` sessions, the /mcp bridge, stream parsing, control
agents/browser.py  browser-use over CDP (used by the browse tool); ask_human in bounded waits; screenshots;
                   card placeholders only after approval
registry.py        Toolsets: built-in, plugin files (./plugins), MCP servers; planner tool selection
tools/sandbox_tools.py  `sandbox` toolset: shell, files, git_push, gh and cli (signed in per command)
connect.py         Sign in once: connect a service's CLI with the browser session (7 CLIs), run connected CLIs
tools/cli_login.py cli_login: the agent side of connect.py
tools/browser_tools.py  `browser` toolset (API engine): browse(task, why_not_api, payment…) — last resort
tools/browser_direct.py `browser` toolset (Claude Code engine): browser_start … browser_done, step by step
tools/infra.py     `vercel`, `github`, `vault` toolsets
tools/web.py       `web` toolset: fetch_url, api_request (vault secrets injected, host-bound for protected ones)
integrations.py    API-first routing catalog (~27 services) + find_integrations (every agent and the planner)
tools/human.py     ask_human, request_approval (every agent)
accounts.py        Account catalog (~70 services), status detection, sign in/out, `accounts` toolset
cdp.py             Minimal CDP client: cookies, open tab, sign out, probe a page
sdk.py             Public plugin API: todd_tool(toolset=, planner=), get_ctx, get_agent_id, ToolError, …
prompts.py         Prompt resolution (dashboard → ./prompts file → built-in) + {{var}} rendering
policy.py          Spend policy + ledger; the only path to spending money
runtime.py         RunContext: events, human-in-the-loop waits, current agent id, pause gates, cancellation
llm.py             Model tiers via LiteLLM (LangChain ChatLiteLLM), optional native reasoning, cost tracking
vault.py           Fernet-encrypted secrets in Postgres (env var fallback), output scrubbing
```

## A run, end to end

1. `POST /api/runs` (via the web proxy, which adds the API token) stores a `Run`. `RunManager.start()`
   creates an asyncio task with its own `RunContext` (a contextvar, so every tool can call `get_ctx()`; a
   second contextvar holds the id of the agent currently running).
2. The run's toolsets are assembled (built-in + plugins + MCP servers). The planner graph gets the
   orchestration tools, human tools and planner-flagged tools (`check_accounts`, `request_signins`,
   `fetch_url`, `vault_list`…). Its prompt lists every toolset, the budget, integrations and account status.
   It's invoked with `thread_id = run.id` on the Postgres checkpointer.
3. The planner routes API-first (`find_integrations` for every service involved), preflights accounts only
   for services that will go through the browser (`check_accounts`, then one `request_signins`), then
   **designs agents around groups of related work** (typically 1–4; `limits.max_concurrent_agents` = 4 and
   `limits.max_agents_per_run` = 12 by default): `spawn_agent(name, instructions, task, tasks, toolsets,
   model, background)` — `tasks` becomes a numbered checklist in the agent's task — creates an
   `AgentInstance` row, renders the `worker` prompt with the planner's instructions and the toolsets' docs
   and tips, builds a LangGraph agent with those tools, and runs it as its own asyncio task. Spawns return an
   id right away (`background=false` waits for the summary instead); `wait_for_agents` collects results. Any
   wait ends early when the human messages the planner, so it can act (spawn more, redirect, cancel) while the
   agents keep running. The planner can't `finish` while agents are running (that would stop them).
4. Every event carries the agent id. The dashboard's windows view shows one window per agent: its thinking,
   thoughts, tool calls, browser steps, human replies, and a message box (`POST /runs/{id}/message` with
   `agent_id`). Agents read messages on their next step; a message to a paused agent also resumes it.
   **Pause:** each agent has a `PauseGate` in the `RunContext`; `call_model` and `call_tools` await it, and a
   running browser-use task is mirrored onto `agent.pause()/resume()`. `POST /runs/{id}/agents/{aid}/pause|resume`
   (the planner's id is `planner`) and `/pause-all|/resume-all`. Stopping a run releases every gate.
5. Anything needing a person (`ask_human`, `request_approval`, a spend above the policy) creates an
   `Interaction` owned by that agent, sets the run to `waiting`, and awaits a future that
   `POST /api/interactions/{id}` resolves. Stopping an agent closes its open interactions.
6. Every agent ends with `finish(summary, success)`; the summary (Done / Outputs / How / Left-needs-you) is
   emitted as a `summary` event and shown as a card. If an agent is cancelled, fails or finishes without one,
   a fallback summary is built from its tool calls and last message. The planner's `finish` emits the run
   summary (with an Agents line), and any agents still running are stopped.
7. If the process dies, runs and agents are marked `interrupted` at startup. **Resume** continues the planner
   from its last checkpoint. Background agents from before the restart are gone; the planner re-spawns what
   it still needs.

## Engines

`settings.engine` picks what runs the agents; it's fixed for the life of a run.

**`claude_code` (default, "Your Claude plan")** — `agents/claude_code.py`. Every agent, the planner included, is a
headless Claude Code session: `claude -p --input-format stream-json --output-format stream-json --model <tier>
--system-prompt <Todd prompt> --tools "" --strict-mcp-config --mcp-config <todd>`. Usage counts toward the Claude
plan the CLI is signed in with (`docker compose exec -it api claude auth login`; credentials stay in
`CLAUDE_CONFIG_DIR` on the data volume and never pass through Todd).

- **Tools:** the API serves each session an MCP endpoint, `POST /mcp/{token}` (JSON-RPC over streamable HTTP,
  loopback only, an unguessable token per agent session). It lists the agent's Todd tools plus `finish`, and
  runs calls through the same `execute_tool_calls` path as the other engine (events, scrubbing, spend policy,
  approvals). All built-in Claude Code tools are disabled. `MCP_TOOL_TIMEOUT` is raised so approvals can wait.
- **Streaming:** `assistant` blocks become `thinking` / `thought` events; tool events come from the MCP side.
- **Control:** pause holds the next tool call (and the next turn); human/planner messages are appended to the
  agent's next tool result, or sent as a new turn; a turn that ends without `finish` gets a nudge (3 max);
  cancel kills the process group right away; auth failures (`api_retry` 401/403) fail fast with sign-in steps;
  rate-limit retries show up as status lines.
- **Resume:** the planner's session id is `uuid5(run id)`, so Resume continues the same conversation with
  `--resume`.
- **Browser:** the `browser` toolset becomes `tools/browser_direct.py`: `browser_start(why_not_api, …)`,
  `browser_navigate/click/type/keys/select/scroll/back/switch_tab/search/wait/state`, `browser_done`, built on
  browser-use's primitives over CDP (indexed elements, screenshots returned to the model as images). Same run-wide
  lock, spend approval and card placeholders (`<secret>card_number</secret>`, filled in only on approved domains,
  screenshots off) as `browse`.
- **Tests:** `tests/test_claude_code.py` runs the real CLI against `tests/fake_anthropic.py`, a scripted Messages
  API (`TODD_TEST_ANTHROPIC_BASE_URL`), end to end.

**`api` ("API keys")** — the LangGraph graph described above, models via LiteLLM, `browse` via browser-use with
its own LLM, LangGraph checkpoints for resume.

## Accounts

The agents share one persistent Chromium profile. `accounts.py` has a catalog of services with a login URL,
a check URL (a page that needs a login), cookie domains, and (where known) the auth cookie names.

- **Session cookie** (instant): `Storage.getCookies` over CDP; a live, unexpired auth cookie means signed in.
  Its expiry is shown as "session ~Nd left".
- **Verified by visit**: load the check URL in a background tab; signed out if it lands on a login URL or
  shows a visible password field; unknown if the page can't load. The result is remembered.
- **Via**: Firebase, GCP, Gmail, Play Console, … inherit the Google account's status; App Store Connect
  inherits Apple.
- **Sign out** expires the service's cookies. **Custom sites** can be added from the dashboard.
- The onboarding wizard and "Sign in to missing" open each login page in the live browser and advance when
  the sign-in is detected.
- **Sign in once:** if the service has a CLI Todd can connect (see `connect.CONNECTORS`), the wizard connects it
  with the fresh session before moving on, so the human is still there if the site asks for 2FA. Each account card
  shows its CLI and a Connect button; agents can do the same mid-run with `cli_login`, which asks the human once
  only if a password/2FA page appears.

## Spend safety

- `authorize_spend()` auto-approves only if the amount is ≥ $0.01, ≤ `auto_approve_under_usd`, ≤ the
  remaining run budget, not `always_ask`, not a card payment, and not a duplicate of an earlier spend in the
  run (same merchant and amount, which guards against double charges after a resume). Anything else becomes
  a `spend` interaction.
- Charging a run is atomic: the run row is locked, then re-checked, recorded and updated. Parallel approvals
  can't push the books out of line.
- Spends are recorded as `authorized`, then settled `completed` / `failed` (refunded to the budget) /
  `needs_review` (the outcome is unknown, e.g. a timeout or 5xx after a purchase request; a human checks).
- Browser card payments **always** need a human. The planner must pass `payment_amount_usd` +
  `payment_domains` (exact domains, no wildcards). Only after approval does the browser agent get
  `sensitive_data` scoped to those domains; browser-use swaps placeholders for the real values at typing
  time. While card details are in use, screenshots and vision are off.

## Security model

- **Single-tenant and local:** the dashboard and live view bind to 127.0.0.1. The dashboard has no login yet.
- **Network isolation:** `postgres` and `web` sit only on `core`. `sandbox` (agent-run code) and `browser`
  (untrusted web pages) each share a network only with `api`, so they can't reach the DB, the dashboard or
  each other (no CDP cookie theft from the sandbox).
- **API token:** every `/api/*` route except health requires a token that the API generates on first start
  into a volume mounted only by `api` and `web` (or set `TODD_API_TOKEN`). Code in the sandbox, or a page in
  the browser, can reach the API port but can't approve spends, change settings or read secrets. OpenAPI
  docs are disabled.
- **Secrets never enter prompts:** tools read them from the vault. Every tool result and error is scrubbed of
  all vault values before it reaches the model or the timeline. `{{secret:NAME}}` injection is refused for
  protected secrets (integration tokens, model keys, card fields) and for public env vars (`NEXT_PUBLIC_*`,
  `VITE_*`…). Agents can't overwrite protected secrets.
- **git_push:** validates `owner/name` and the branch; refuses repos whose local git config could redirect
  the push or run code (`url.*`, `credential.*`, `core.hooksPath`, `http.*`, `include.*`…); disables hooks;
  passes the token as an env-only HTTP header and scrubs it (and its base64 form) from output. Known limit:
  the sandbox runs as a single user, so code already running in the sandbox *during* a push could read the
  push process's environment. A per-push user or a credential proxy is the next step.
- **CLI sign-ins (connect.py):** a CLI's browser login runs in the sandbox with a private `HOME`. `cdp.approve`
  approves it in the shared browser with generic page rules: fill the one-time code, click Continue/Authorize
  with real mouse events, only on the provider's own domains; never a Cancel/Deny/switch-account button, never on
  a password, 2FA or confirm-access page (those go to the human, in the same tab). A redirect to the CLI's
  `localhost` callback (Wrangler) is caught before the browser loads it and replayed inside the sandbox, and a code
  the page shows (Firebase) is read by Todd and handed to the CLI. The model never sees any of it. The result goes
  in the vault: the token itself when the CLI prints it (`gh auth token` → `GITHUB_TOKEN`, which also powers the API
  toolset and `git_push`), otherwise an encrypted snapshot of the CLI's sign-in files (`CLI_STATE_<SERVICE>`,
  protected). `cli` unpacks the snapshot into a private temp dir for one command, saves refreshed tokens back and
  deletes the dir; subcommands that sign in/out or print credentials are blocked (`gh`: `auth`, `extension`,
  `alias`, `config`, `--hostname`). Known limit, as for `git_push`: code already running in the sandbox during a CLI
  command could read those files. `browser_save_secret` moves a key a page shows into the vault without the model
  seeing it.
- **Human waits:** questions stay open across bounded waits (browser-use caps each action at ~180s, so the
  browser agent calls `wait_for_human` in 150s slices). At startup, interactions left over from a dead
  process are closed.
- Prompt injection: the harness prompt tells agents to treat page/tool content as data. More importantly,
  money and irreversible actions are gated by policy code, not by the model.

## Extending

- **Toolset:** a `.py` file in `./plugins` with `@todd_tool(toolset="name")` functions (Google-style
  docstrings become the schema; the module docstring describes the toolset to the planner). Use
  `authorize_spend` for anything that costs money.
- **MCP:** Settings → Toolsets & plugins → MCP servers JSON; each server becomes toolset `mcp_<name>`.
  `{{secret:NAME}}` is allowed in headers.
- **Prompts:** `./prompts/<harness|planner|worker|browser>.md` or the dashboard.
- **New kinds of agents** need no code: the planner designs them from the available toolsets. To give agents
  a new capability, add a toolset.
