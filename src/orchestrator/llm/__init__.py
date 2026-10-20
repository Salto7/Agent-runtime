"""Pluggable LLM providers for Peon (orchestrator library — no Django).

Default module: LiteLLM in-process. Optional OpenAI-compatible proxy exposure
is controlled via env/settings (``LLM_PROXY_ENABLED``), not here.
"""

from __future__ import annotations

from orchestrator.llm.provider_base import LLMProviderBase
from orchestrator.llm.registry import get_llm_provider, reset_llm_provider

__all__ = [
    "LLMProviderBase",
    "get_llm_provider",
    "reset_llm_provider",
]
