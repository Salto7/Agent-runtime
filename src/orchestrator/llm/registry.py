"""Resolve LLM provider modules from RuntimeConfig."""

from __future__ import annotations

from orchestrator.config import get_config
from orchestrator.llm.provider_base import LLMProviderBase
from orchestrator.llm.providers.litellm import LiteLLMProvider

_PROVIDERS: dict[str, type[LLMProviderBase]] = {
    "litellm": LiteLLMProvider,
}

_cached: LLMProviderBase | None = None
_cached_id: str | None = None


def reset_llm_provider() -> None:
    """Drop cached provider (call after ``configure()``)."""
    global _cached, _cached_id
    _cached = None
    _cached_id = None


def get_llm_provider() -> LLMProviderBase:
    """Return the configured provider module (default: litellm)."""
    global _cached, _cached_id
    module_id = (get_config().llm_module or "litellm").strip().lower() or "litellm"
    if _cached is not None and _cached_id == module_id:
        return _cached
    cls = _PROVIDERS.get(module_id)
    if cls is None:
        known = ", ".join(sorted(_PROVIDERS))
        raise RuntimeError(
            f"Unknown LLM_MODULE={module_id!r}; known modules: {known}"
        )
    _cached = cls()
    _cached_id = module_id
    return _cached
