# Changelog

All notable changes to Todd are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may include breaking
changes).

## [Unreleased]

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
