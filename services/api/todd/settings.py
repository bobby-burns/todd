"""User-editable settings stored in the database, with defaults."""

from __future__ import annotations

import copy
from typing import Any

from .db import Setting, select, session

DEFAULTS: dict[str, Any] = {
    # Any LiteLLM model string: "anthropic/...", "openai/...", "gemini/...", "openrouter/...",
    # "ollama_chat/qwen3:14b", "groq/...", "deepseek/..." etc.
    # Model tiers. The planner uses "planner"; spawned agents pick a tier (worker = default, planner = "strong",
    # fast = cheap/quick); the browser toolset's browser-use loop uses "browser".
    "models": {
        "planner": "anthropic/claude-sonnet-5",
        "worker": "anthropic/claude-sonnet-5",
        "fast": "anthropic/claude-haiku-4-5-20251001",
        "browser": "anthropic/claude-sonnet-5",
    },
    # Which engine runs the agents. "claude_code": every agent is a headless Claude Code session signed in with the
    # human's Claude plan (usage counts toward the plan). "api": LangGraph + LiteLLM with provider API keys.
    "engine": "claude_code",
    "claude_code": {
        "models": {"planner": "claude-opus-5-5", "worker": "claude-opus-5-5", "fast": "claude-haiku-4-5-20251001"},
    },
    # Native model reasoning ("thinking"). Agents always narrate their reasoning in text; this additionally asks
    # models that support it (Claude, o-series, Gemini, DeepSeek…) for extended reasoning, shown in the dashboard.
    "thinking": {"enabled": False, "effort": "medium"},
    "limits": {"max_concurrent_agents": 4, "max_agents_per_run": 12},
    # Accounts the user picked in onboarding / on the Accounts page, and custom sites they added.
    "accounts_selected": [],
    "cli_auto_skip": [],  # CLIs the human disconnected: don't connect them again automatically
    "accounts_custom": [],
    "accounts_manual": {},  # id -> {"signed_in": bool, "at": iso} manual confirmations
    "onboarding_completed": False,
    # Base URL for local models (Ollama / LM Studio / vLLM OpenAI-compatible servers).
    "ollama_base_url": "http://ollama:11434",
    "openai_compatible_base_url": "",
    "spend_policy": {
        "auto_approve_under_usd": 15.0,
        "default_run_budget_usd": 50.0,
        "always_ask": False,
    },
    "integrations": {
        "vercel_team_id": "",
        "github_owner": "",  # empty -> the token's user
    },
    # Dashboard prompt overrides (empty -> file in ./prompts or built-in default). See todd/prompts.py.
    "prompts": {"harness": "", "planner": "", "worker": "", "browser": ""},
    # MCP servers whose tools are given to the planner, in langchain-mcp-adapters format, e.g.
    # {"linear": {"transport": "streamable_http", "url": "https://mcp.linear.app/mcp", "headers": {...}}}
    # Header values may use {{secret:NAME}}.
    "mcp_servers": {},
    # Registrant contact for domain purchases (required by registrars).
    "registrant_contact": {
        "firstName": "",
        "lastName": "",
        "email": "",
        "phone": "",
        "address1": "",
        "city": "",
        "state": "",
        "zip": "",
        "country": "US",
        "companyName": "",
    },
}


REPLACE_KEYS = {"mcp_servers", "accounts_selected", "accounts_custom", "accounts_manual", "cli_auto_skip"}  # updated wholesale instead of deep-merged


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _merge(base.get(k), v) if k in base else v
        return out
    return copy.deepcopy(override) if override is not None else copy.deepcopy(base)


def get_all() -> dict[str, Any]:
    with session() as s:
        rows = {r.key: r.value for r in s.exec(select(Setting)).all()}
    out = {k: _merge(v, rows.get(k)) for k, v in DEFAULTS.items()} | {
        k: v for k, v in rows.items() if k not in DEFAULTS
    }
    models = out["models"]
    if "coder" in models:  # v0.1 name for the worker tier
        stored = (rows.get("models") or {})
        if "worker" not in stored:
            models["worker"] = models["coder"]
        models.pop("coder", None)
    return out


def get(key: str) -> Any:
    return get_all().get(key)


def update(patch: dict[str, Any]) -> dict[str, Any]:
    current = get_all()
    with session() as s:
        for key, value in patch.items():
            if key in REPLACE_KEYS or key not in current:
                merged = value
            else:
                merged = _merge(current.get(key), value)
            row = s.get(Setting, key) or Setting(key=key)
            row.value = merged
            s.add(row)
        s.commit()
    return get_all()
