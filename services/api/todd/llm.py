"""Bring-your-own-model layer. Everything goes through LiteLLM so any provider (or a local model) works.

Roles: planner (orchestrator), browser (browser-use), coder (code agent). Each role maps to any LiteLLM
model string in Settings. Provider API keys come from the vault (or env vars of the same name).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import litellm

from . import settings, vault

litellm.drop_params = True  # tolerate provider-specific params
litellm.suppress_debug_info = True
litellm.modify_params = True  # e.g. keeps Anthropic thinking + tool-use histories valid

# LiteLLM provider -> name of the secret holding its API key
PROVIDER_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    "together_ai": "TOGETHERAI_API_KEY",
    "fireworks_ai": "FIREWORKS_AI_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "vercel_ai_gateway": "AI_GATEWAY_API_KEY",
}
LOCAL_PROVIDERS = {"ollama", "ollama_chat"}


@dataclass
class ModelSpec:
    role: str
    model: str
    provider: str
    api_key: str | None
    api_base: str | None

    def kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model}
        if self.api_key:
            kw["api_key"] = self.api_key
        if self.api_base:
            kw["api_base"] = self.api_base
        return kw


def provider_of(model: str) -> str:
    try:
        return litellm.get_llm_provider(model)[1]
    except Exception:
        return model.split("/", 1)[0] if "/" in model else "openai"


TIERS = {"default": "worker", "worker": "worker", "strong": "planner", "planner": "planner", "fast": "fast",
         "browser": "browser", "coder": "worker"}


def model_for(role: str) -> ModelSpec:
    cfg = settings.get_all()
    role = TIERS.get(role, role)
    model = cfg["models"].get(role) or cfg["models"].get("worker") or cfg["models"]["planner"]
    provider = provider_of(model)
    api_key = None
    api_base = None
    if provider in LOCAL_PROVIDERS:
        api_base = cfg.get("ollama_base_url") or os.getenv("OLLAMA_API_BASE")
    elif provider == "openai" and cfg.get("openai_compatible_base_url"):
        api_base = cfg["openai_compatible_base_url"]
        api_key = vault.get_secret("OPENAI_COMPATIBLE_API_KEY") or vault.get_secret("OPENAI_API_KEY") or "none"
    if api_key is None and provider in PROVIDER_KEYS:
        api_key = vault.get_secret(PROVIDER_KEYS[provider])
    return ModelSpec(role=role, model=model, provider=provider, api_key=api_key, api_base=api_base)


ChatFactory = Callable[..., Any]  # (role, agent_name) -> chat model
_factory: ChatFactory | None = None


def set_chat_model_factory(fn: ChatFactory | None) -> None:
    """Tests use this to plug in a scripted fake chat model."""
    global _factory
    _factory = fn


def make_chat_model(role: str, agent_name: str | None = None) -> Any:
    """A LangChain chat model for the role/tier, backed by LiteLLM (any provider, incl. local)."""
    if _factory is not None:
        return _factory(role, agent_name)
    from langchain_litellm import ChatLiteLLM

    spec = model_for(role)
    thinking = settings.get("thinking") or {}
    model_kwargs: dict[str, Any] = {}
    if thinking.get("enabled"):
        # LiteLLM maps reasoning_effort to each provider's native option (Anthropic thinking budget, etc.)
        model_kwargs["reasoning_effort"] = thinking.get("effort", "medium")
    if spec.provider in PROVIDER_KEYS and not spec.api_key:
        raise RuntimeError(f"No API key for {spec.provider} (model {spec.model} for the {role} role). Add "
                           f"{PROVIDER_KEYS[spec.provider]} in Settings → Models, or pick a different model.")
    return ChatLiteLLM(model=spec.model, api_key=spec.api_key, api_base=spec.api_base, max_tokens=16000,
                       max_retries=3, request_timeout=300, model_kwargs=model_kwargs)


def cost_of(role: str, usage: dict | None) -> float:
    """USD cost of a call from LangChain usage_metadata (0 if the model has no public pricing)."""
    if not usage:
        return 0.0
    try:
        p, c = litellm.cost_per_token(model=model_for(role).model, prompt_tokens=int(usage.get("input_tokens", 0)),
                                      completion_tokens=int(usage.get("output_tokens", 0)))
        return float(p + c)
    except Exception:
        return 0.0


async def ping(role: str) -> str:
    """Quick connectivity check used by Settings → Test."""
    spec = model_for(role)
    resp = await litellm.acompletion(**spec.kwargs(), messages=[{"role": "user", "content": "Reply with: pong"}],
                                     max_tokens=10)
    return (resp.choices[0].message.content or "").strip()
