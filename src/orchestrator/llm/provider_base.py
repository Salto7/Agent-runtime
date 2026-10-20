"""library LLM provider ABC — all backends inherit this interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from orchestrator.config import get_config


class LLMProviderBase(ABC):
    """Unified LLM backend interface.

    Call sites use ``orchestrator.utils.llm`` (or ``get_llm_provider()``).
    New providers subclass this ABC and register in ``orchestrator.llm.registry``.
    """

    #: Stable module id (matches ``LLM_MODULE`` env / settings).
    id: ClassVar[str] = ""

    @abstractmethod
    def configured(self) -> bool:
        """True when credentials/config allow chat calls."""

    @abstractmethod
    def complete(self, messages: list[dict[str, str]]) -> str:
        """Run a chat completion; return assistant text."""

    def describe(self) -> dict[str, Any]:
        """Operator-facing snapshot. Subclasses may extend via ``_describe_extra``."""
        cfg = get_config()
        out: dict[str, Any] = {
            "module": self.id,
            "configured": self.configured(),
            "proxy_enabled": bool(cfg.llm_proxy_enabled),
            "proxy_url": (cfg.llm_proxy_url or "").strip() or None,
        }
        out.update(self._describe_extra())
        return out

    def _describe_extra(self) -> dict[str, Any]:
        """Provider-specific describe fields (model, upstream, …)."""
        return {}
