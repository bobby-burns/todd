# Changelog

All notable changes to Todd are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may include breaking
changes).

## [Unreleased]

### Added

- Sign in once: signing in to GitHub, Vercel, Netlify, Railway, Cloudflare, Stripe or Firebase (Accounts page,
  Setup) also signs in that service's CLI with the same session. Todd approves it in the browser itself and only
  asks you for a password/2FA page. Agents can do the same with `cli_login`.
- `cli` tool: run a connected CLI (`cli("vercel", "deploy --prod --yes")`) with its saved sign-in.
- `gh` tool: the GitHub CLI in the sandbox, signed in per command from the vault. The sandbox image now includes `gh`.
- `browser_console`: JavaScript errors, console warnings and failed requests on the current page, for agents
  checking the sites they build. The prompts now say verifying your own work is a normal use of the browser.
- `browser_read_text`: copy the full text of an element or page (shadow DOM and field values included, never
  password fields) into the agent's work.
- `browser_save_secret`: save a key a page shows straight into the vault without the model seeing it.
- Continue a finished run: message its planner (in its window, the run summary or the timeline) and it picks its
  conversation back up, on both engines.
- Rename, run again, copy the prompt of, stop or delete a run from the sidebar or the runs list; **Edit** in the
  sidebar deletes several at once. Deleting keeps ledger entries and workspace files.
- Finished, failed and stopped agents fold into a compact "Finished agents" tray on the run page. Open one to see
  its window again, or hide it.
- **Take control** opens the agents' browser in a large window you can click and type in. Sign-in prompts and CLI
  rows that need you have an **Open browser** button.

### Changed

- `spawn_agent` runs agents in the background by default, and `wait_for_agents` ends early when you message the
  planner, so it can act on the message while agents keep working.
- Sending a message to a paused agent (or the planner) resumes it.
- The planner can't finish while agents it spawned are still running.
- A signed-in account's CLI now connects automatically when the Accounts page or Setup loads (one at a time, once per
  start; the button retries). Disconnecting a CLI turns this off for it.
- CLI approval: when a site keeps its final Authorize/Allow button disabled until a person interacts, Todd scrolls to
  it, outlines it and asks you for that one click instead of waiting. After you finish a password or 2FA page, Todd
  takes over again on the next page.
- `find_integrations` routes a service that isn't connected but has a browser-session login to `connect`
  (`cli_login`). Agents are told never to create tokens in the browser or copy credentials through their context.

### Fixed

- The planner no longer blocks on a sub-agent: messages sent while it waited weren't seen until the agent finished.
- `find_integrations` reported GitHub as "ready" without a `GITHUB_TOKEN` (the built-in `github` toolset was
  mistaken for a plugin).
- Sandbox commands that left a process running in the background (dev servers, CLI logins) hung until their
  timeout, because leaked descriptors kept the output stream open.
- A sandbox command that timed out lost everything it had printed; the output is now kept.
- The sandbox runs with an init process, so exited background processes are reaped.
- **Take control** didn't let you control the browser (only the new-tab view did): noVNC treats `view_only=0` as
  on. The Setup and Accounts browsers were affected too.
- Vercel's device-code field (`autocomplete="one-time-code"`) was mistaken for a 2FA prompt, so the Vercel CLI
  sign-in stopped for you right away.

## [0.4.0] - 2026-09-25

First public release.

### Added

- Dynamic multi-agent planner: `spawn_agent` designs agents around groups of related work and runs them in
  parallel, with `wait_for_agents`, `message_agent` and `cancel_agent`.
- Two engines: **Your Claude plan** (headless Claude Code sessions, the default) and **API keys** (any
  LiteLLM model per tier, including local models via Ollama).
- Toolsets: code sandbox, signed-in browser (browser-use over CDP with a noVNC live view), Vercel, GitHub,
  web/API requests, vault, accounts, plugin files and MCP servers.
- API-first routing through `find_integrations`, with the browser as a last resort (`why_not_api`).
- Accounts catalog of about 70 services, with login detection and a setup wizard.
- Spend policy and ledger: auto-approve limit, per-run budgets, duplicate-charge guard, and human approval
  for all card payments.
- Approval flow for public actions (posting, messaging, publishing).
- Dashboard with one live window per agent, including reasoning, tool calls, browser steps, pause/resume,
  steering messages and summary cards.
- Crash-safe runs via LangGraph + Postgres checkpoints, with Resume.
- Editable prompts (`./prompts` or the dashboard).

### Fixed

- The API image failed to install its dependencies: `browser-use` 0.13.10 pins `mcp==2.1.1`, which
  conflicts with `langchain-mcp-adapters` 0.3.2 (`mcp<2`). Pinned `browser-use` to 0.13.9.

[Unreleased]: https://github.com/bobby-burns/todd/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/bobby-burns/todd/releases/tag/v0.4.0
