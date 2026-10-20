"""Learn / authoring helpers (tool catalog suggestions + skill scaffolding)."""

from __future__ import annotations

from typing import Any

__all__ = ["LearnAuthoring"]


def __getattr__(name: str) -> Any:
    if name == "LearnAuthoring":
        from orchestrator.learn.authoring import LearnAuthoring

        return LearnAuthoring
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
