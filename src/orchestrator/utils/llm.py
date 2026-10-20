"""LLM config and short invoke helpers (facade over provider modules — no Django)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from orchestrator.config import get_config
from orchestrator.llm import get_llm_provider
from orchestrator.utils.strings import extract_json

LLM_NOT_CONFIGURED = (
    "LLM not configured — set OPENROUTER_API_KEY (or OpenAI/LiteLLM)."
)


@dataclass(frozen=True)
class LLMConfig:
    """Snapshot of upstream credentials (not the provider module id)."""

    provider: str
    model: str
    temperature: float
    api_key: str
    api_base: str | None = None
    max_tokens: int | None = None
    module: str = "litellm"


def llm_config() -> LLMConfig:
    cfg = get_config()
    return LLMConfig(
        provider=cfg.llm_provider,
        model=cfg.litellm_model,
        temperature=float(cfg.llm_temperature or 0.0),
        api_key=cfg.litellm_api_key or "",
        api_base=cfg.litellm_api_base or None,
        max_tokens=cfg.llm_max_tokens,
        module=(cfg.llm_module or "litellm").strip().lower() or "litellm",
    )


def llm_configured() -> bool:
    return get_llm_provider().configured()


def require_llm() -> None:
    """Raise ``RuntimeError`` when no upstream LLM credentials are configured."""
    if not llm_configured():
        raise RuntimeError(LLM_NOT_CONFIGURED)


def chat_messages(messages: list[dict[str, str]]) -> str:
    """Chat completion from role/content dicts (system / user / assistant)."""
    return get_llm_provider().complete(messages)


def chat_text(system: str, human: str) -> str:
    return chat_messages(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": human},
        ]
    )


def chat_json(system: str, human: str) -> dict:
    return json.loads(extract_json(chat_text(system, human)))
