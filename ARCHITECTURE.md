# Todd architecture (v0.4)

## Services (docker compose)

| Service    | What it is                                                                 | Exposed            |
|------------|----------------------------------------------------------------------------|--------------------|
| `web`      | Next.js 16 dashboard. Proxies `/api/*` to the API, including SSE streams    | 127.0.0.1:3000     |
| `api`      | FastAPI + LangGraph orchestrator, tools, vault, spend policy                | internal (token)   |
| `postgres` | Runs, events, approvals, vault, ledger, settings, LangGraph checkpoints     | internal           |
| `sandbox`  | node 22 / pnpm / git / python / vercel + firebase + eas CLIs, with a small exec API | internal   |
| `signedin` | Same image and workspace; runs only the commands that use your sign-ins     | internal (token)   |
| `media`    | Image embeddings (CLIP, fastembed on CPU), captioned slides (Pillow) and MP4 slideshows (FFmpeg) for the video toolset; shares the workspace | internal (token) |
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
tools/sandbox_tools.py  `sandbox` toolset: shell, files, git, git_push, gh, cli and eas (signed in per command)
connect.py         Sign in once: connect a service's CLI with the browser session (7 CLIs), run connected CLIs
tools/cli_login.py cli_login: the agent side of connect.py
tools/browser_tools.py  `browser` toolset (API engine): browse(task, why_not_api, payment…) — last resort
tools/browser_direct.py `browser` toolset (Claude Code engine): browser_start … browser_done, step by step
tools/infra.py     `vercel`, `github`, `vault` toolsets
tools/web.py       `web` toolset: fetch_url, api_request (vault secrets injected, host-bound for protected ones)
tools/video.py     `video` toolset: storyboard (video/<slug>/storyboard.json), search each shot by meaning (Pexels +
                   the run's own images) ranked with the picked shots in view, pick, render captioned slides + MP4
tools/media.py     Client for the media container (embeddings, fetch, slide composition, MP4 render)
media_index.py     Nearest-neighbour search over MediaAsset embeddings: pgvector when the DB has it, else Python
integrations.py    API-first routing catalog (~27 services) + find_integrations (every agent and the planner)
tools/human.py     ask_human, request_approval, authorize_purchase (every agent)
gates.py           what needs a person, checked in code: purchases, card entry, public actions, going live, public repos
launch.py          the launch plan: asked at the start of a website run (address, repo visibility, going live)
readiness.py       the "Ready for real visitors?" checklist, read from a finished run's project files
accounts.py        Account catalog (~70 services), status detection, sign in/out, `accounts` toolset
cdp.py             Minimal CDP client: cookies, open tab, sign out, probe a page
sdk.py             Public plugin API: todd_tool(toolset=, planner=), get_ctx, get_agent_id, ToolError, …
prompts.py         Prompt resolution (dashboard → ./prompts file → built-in) + {{var}} rendering
policy.py          Spend policy + ledger; the only path to spending money
runtime.py         RunContext: events, human-in-the-loop waits, current agent id, pause gates, cancellation
llm.py             Model tiers via LiteLLM (LangChain ChatLiteLLM), optional native reasoning, cost tracking
vault.py           Fernet-encrypted secrets in Postgres (env var fallback), output scrubbing; each secret is credited
                   to the run and agent that saved it (SecretOrigin), so Settings → Vault groups keys by run
usage.py           Model usage per run/agent/model/day (both engines) + tool calls and browser steps from events
redact.py          Secret-shaped strings (tokens, keys, URL credentials; stricter on web pages) hidden from models
preview.py         localhost:PORT in the agents' browser → the same port in the sandbox (relayed, no deploy needed)
tools/page_guard.py  Blur secret-looking text before screenshots; redact the browser-use agent's state messages
workspace.py       The run's folder in the sandbox, read-only for the dashboard's Files view (sandbox /files/tree
                   and /files/view): vault values masked, `.env` values hidden until asked, private keys never shown
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
8. A run that has ended (any status) continues when the human messages its planner: `RunManager.start(run_id,
   followup=…)` reopens the planner's conversation (the same LangGraph thread with `done` reset, or the same Claude
   Code session with `--resume`) with the message and a note that earlier agents have stopped. `DELETE /runs/{id}`
   stops a run if needed and removes its events, agents, interactions, screenshots, checkpoints and Claude Code
   session files; ledger entries and workspace files stay. `PATCH /runs/{id}` renames it.
9. The run's files: every agent in a run works in `/workspace/<run_id>` in the sandbox. `GET /runs/{id}/files`
   lists it, `…/files/view?path=` shows one file, `…/files/download?path=` downloads it and `…/files/media?path=`
   streams a video for the Files view's player (inline, single byte ranges; read-only; confined to that folder,
   symlinks included). `GET /secrets?run_id=` lists the vault keys that run saved.

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
- **Resume:** the planner's session id is `uuid5(run id)`, so Resume (and a follow-up message to a finished run)
  continues the same conversation with `--resume`.
- **Browser:** the `browser` toolset becomes `tools/browser_direct.py`: `browser_start(why_not_api, …)`,
  `browser_navigate/click/type/keys/select/scroll/back/switch_tab/search/wait/state`, `browser_done`, built on
  browser-use's primitives over CDP (indexed elements, screenshots returned to the model as images). Same browser
  lock, gates, spend approval and card placeholders (`<secret>card_number</secret>`, filled in only on approved domains,
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
  `payment_domains` (exact sites: no wildcards, no subdomains, `www.` counts as the site). Only after approval does
  the browser agent get `sensitive_data` scoped to those sites; browser-use swaps placeholders for the real values
  at typing time. While card details are in use, screenshots and vision are off. A shared payment page
  (checkout.stripe.com, paypal.com…) must come with the merchant's own site, and gets the card only when the tab
  came from that site (its history, or the tab that opened it). The page's total must fit the approved amount.
- **Gates** (`gates.py`) sit in front of every browser action (`gates.install` wraps browser-use's
  `execute_action`, so both engines go through them), `api_request`, `cli`, `gh`, `eas` and `shell`: purchase-looking
  clicks, payment and billing pages, and known purchase APIs and commands need `authorize_purchase` (human-approved,
  in the Ledger, 30 minutes, that agent and site); posting/messaging/email sites need `request_approval(sites=…)`;
  known posting APIs and publishing commands open an approval with the exact request. Recognition is by label,
  page and endpoint, so these are backstops to the budget and a limited card, not a proof.
- **Deploying** (`gates.approve_live`): every deploy, production (`vercel --prod`, a plain `vercel deploy` (a new
  project's first one is its production site), `netlify deploy --prod`, `firebase deploy`, `wrangler deploy`, `railway up`, a
  production EAS update, Vercel's deployments API with `target: production`, `vercel_deploy`) or preview
  (`--target=preview`, Netlify drafts, Firebase channels, Cloudflare preview branches), connecting a domain, a Git
  connection that deploys every push, and Deploy/Publish clicks on hosting dashboards, waits for the human unless
  the launch plan says "when it's ready": approving production covers the run, approving a preview covers previews.
  And deploying is last: refused while another agent with the `sandbox` toolset is still running.
- **One agent per file:** `write_file` records which agent wrote each file; while that agent is running, another
  agent's write to the same file is refused (it's free again once the first finishes). Agents still read each
  other's files: a helper's data file is how it hands work to the builder. New GitHub
  repositories are private (`gh repo create` gets `--private` when no visibility is given); a public one needs the
  launch plan to say public, or an approval.
- **Launch plan** (`launch.py`): for a goal that makes a website, the run's first step (after the sign-in check)
  opens a *background* card (it doesn't set the run to waiting or hold up any agent) asking for the address, the
  repository's visibility and whether to ask before going live. The answer goes to the planner's inbox and to
  `ctx.launch`; a resumed run reads it back from the answered card. Unanswered cards close when the run ends.

## Security model

- **Single-tenant and local:** the dashboard and live view bind to 127.0.0.1. The dashboard has no login yet, so
  its API route only serves the dashboard's own pages: Host must be localhost (or `TODD_ALLOWED_HOSTS`), and
  requests a browser marks as cross-site or from another origin are refused (`web/lib/guard.ts`). No GET starts
  anything. The live view has a password (random unless `VNC_PASSWORD`), shared with the API through a volume and put
  in the dashboard's live-view URL; x11vnc listens only inside the browser container, and Chromium accepts no
  debugging connections from web pages (no `--remote-allow-origins`).
- **Network isolation:** `postgres` and `web` sit only on `core`. `sandbox` (agent-run code) and `browser`
  (untrusted web pages) each share a network only with `api`, so they can't reach the DB, the dashboard or
  each other (no CDP cookie theft from the sandbox). `media` sits only on `media_net` with the API.
- **API token:** every `/api/*` route except health requires a token that the API generates on first start
  into a volume mounted only by `api` and `web` (or set `TODD_API_TOKEN`). Code in the sandbox, or a page in
  the browser, can reach the API port but can't approve spends, change settings or read secrets. OpenAPI
  docs are disabled.
- **Secrets never enter prompts:** tools read them from the vault. Every string in a tool result is scrubbed of
  vault values (and their escaped/encoded forms) before serializing, and of secret-shaped strings (`redact.py`);
  events are scrubbed the same way before they're stored. Browser pages get stricter redaction, and anything
  secret-looking is blurred before a screenshot (`tools/page_guard.py`). `{{secret:NAME}}` injection is refused for
  protected secrets (integration tokens, model keys, card fields) and for public env vars (`NEXT_PUBLIC_*`,
  `VITE_*`…). Agents can't overwrite protected secrets. New secrets go straight to the vault
  (`shell`/`api_request` `save_to_vault`, `ask_human(secret_name=…)`, `browser_save_secret`). See SECURITY.md.
- **git / git_push:** GitHub sign-in reaches git only through gh's credential helper (`GH_TOKEN` in that one
  command's environment), only for network subcommands, with hooks off and after refusing repos whose local config
  could redirect the push or run code (`url.*`, `credential.*`, `core.hooksPath`, `alias.*`, `filter.*`, `http.*`,
  `include.*`…). Global options (`-c`, `-C`), `git config` and program-running options (`--upload-pack`…) are
  refused. The token (and its base64 form) is scrubbed from output.
- **Signed-in runner:** every command that carries a sign-in (git network commands, `gh`, `cli`, `eas`, CLI
  sign-in flows) runs in `signedin`, a second container from the sandbox image with the same workspace volume, its
  own network to the API and a random token the API makes (`SIGNEDIN_URL`; unset, they run in the sandbox). Code
  running in the sandbox can't see its processes, environment or temp files. npm-based CLIs run through
  `todd-cli`, which installs them in the runner's home instead of taking the project's `node_modules/.bin`
  (`npx` would). Known limit: a command that runs the project's own code (a local build, Firebase predeploy hooks,
  Expo's `app.config.js`) runs it with the sign-in present.
- **Media service:** `media` has no vault access, only answers the API (a random token the API makes, `media-token`
  volume), downloads only over https from `MEDIA_FETCH_HOSTS` (default `images.pexels.com`; redirects elsewhere
  refused, 20 MB cap, images only) and reads and writes only inside run folders (symlinks resolved). FFmpeg runs
  with an argument list, opens only local files, gets slides as images the service wrote itself and music only
  through a named audio demuxer (a "song" that is really a playlist is refused).
- **Previews:** the browser's `localhost:PORT` (common dev ports) is relayed through the API to the same port on the
  sandbox's localhost (`preview.py`, relays in the sandbox and browser containers). The sandbox stays off the
  browser's network; only those ports are forwarded, never the sandbox's exec API.
- **CLI sign-ins (connect.py):** a CLI's browser login runs in the sandbox with a private `HOME`. `cdp.approve`
  approves it in the shared browser with generic page rules: fill the one-time code, click Continue/Authorize
  with real mouse events, only on the provider's own domains; never a Cancel/Deny/switch-account button, never on
  a password, 2FA or confirm-access page (those go to the human, in the same tab; once the human is past them,
  Todd carries on). A device-code field that looks like a 2FA field (sized for or holding the code) is filled, not
  handed over. Final buttons that stay disabled until a person interacts (GitHub's focus check, Vercel's Allow
  Access) aren't worked around: Todd scrolls to the button, outlines it and asks the human for that click. When an
  account is signed in and its CLI isn't, the Accounts page calls `POST /api/accounts/auto-connect`, which starts
  the connection (one at a time, once per start, never for a CLI the human disconnected). Approving takes the same
  browser lock as every run. A redirect to the CLI's
  `localhost` callback (Wrangler) is caught before the browser loads it and replayed inside the sandbox, and a code
  the page shows (Firebase) is read by Todd and handed to the CLI. The model never sees any of it. The result goes
  in the vault: the token itself when the CLI prints it (`gh auth token` → `GITHUB_TOKEN`, which also powers the API
  toolset and `git_push`), otherwise an encrypted snapshot of the CLI's sign-in files (`CLI_STATE_<SERVICE>`,
  protected). `cli` unpacks the snapshot into a private temp dir for one command, saves refreshed tokens back and
  deletes the dir; subcommands that sign in/out or print credentials are blocked (`gh`: `auth`, `extension`,
  `alias`, `config`, `--hostname`). All of it runs in the signed-in runner (above). `browser_save_secret` moves a key a page shows into the vault without the model
  seeing it.
- **Human waits:** questions stay open across bounded waits (browser-use caps each action at ~180s, so the
  browser agent calls `wait_for_human` in 150s slices). At startup, interactions left over from a dead
  process are closed.
- **Browser lock:** one lock for the one browser, across every run and the Accounts page's CLI sign-ins
  (`runtime.browser_lock()`), released only by its holder.
- **Web tools** check addresses again when connecting (`web._PublicOnly`), so DNS rebinding can't reach internal
  services.
- Prompt injection: the harness prompt tells agents to treat page/tool content as data. More importantly,
  money and public actions are gated by code (policy and `gates.py`), not by the model.

## Extending

- **Toolset:** a `.py` file in `./plugins` with `@todd_tool(toolset="name")` functions (Google-style
  docstrings become the schema; the module docstring describes the toolset to the planner). Use
  `authorize_spend` for anything that costs money.
- **MCP:** Settings → Toolsets & plugins → MCP servers JSON; each server becomes toolset `mcp_<name>`.
  `{{secret:NAME}}` is allowed in headers.
- **Prompts:** `./prompts/<harness|planner|worker|browser>.md` or the dashboard.
- **New kinds of agents** need no code: the planner designs them from the available toolsets. To give agents
  a new capability, add a toolset.
