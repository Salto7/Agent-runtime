"""In-process LiteLLM provider module (default Peon backend)."""

from __future__ import annotations

import os
from typing import Any, ClassVar

import litellm

from orchestrator.config import LLM_PROVIDER_KEY_ENV, get_config
from orchestrator.llm.provider_base import LLMProviderBase
from orchestrator.utils.strings import plain_text


class LiteLLMProvider(LLMProviderBase):
    """Raw ``litellm.completion`` — talks upstream (OpenRouter, etc.) in-process."""

    id: ClassVar[str] = "litellm"

    def configured(self) -> bool:
        cfg = get_config()
        if (cfg.litellm_api_key or "").strip():
            return True
        # a keyless OpenAI-compatible gateway (local server) is a configured provider too
        base = getattr(cfg, "litellm_api_base", "") or os.environ.get("LITELLM_API_BASE") or os.environ.get("OPENAI_BASE_URL")
        return bool((base or "").strip())

    def _completion_opts(self) -> dict[str, Any]:
        cfg = get_config()
        provider = (cfg.llm_provider or "").strip().lower()
        if (env := LLM_PROVIDER_KEY_ENV.get(provider)) and cfg.litellm_api_key:
            os.environ.setdefault(env, cfg.litellm_api_key)
        opts: dict[str, Any] = {
            "model": cfg.litellm_model,
            "temperature": float(cfg.llm_temperature or 0.0),
        }
        if cfg.litellm_api_key:
            opts["api_key"] = cfg.litellm_api_key
        if cfg.litellm_api_base:
            opts["api_base"] = cfg.litellm_api_base
        if cfg.llm_max_tokens:
            opts["max_tokens"] = cfg.llm_max_tokens
        return opts

    def complete(self, messages: list[dict[str, str]]) -> str:
        resp = litellm.completion(messages=messages, **self._completion_opts())
        choice = (resp.choices or [None])[0]
        if choice is None:
            return ""
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None
        return plain_text(content)

    def _describe_extra(self) -> dict[str, Any]:
        cfg = get_config()
        return {
            "upstream": (cfg.llm_provider or "").strip().lower(),
            "model": cfg.litellm_model,
            "api_base": cfg.litellm_api_base,
        }
