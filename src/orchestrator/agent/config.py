"""Immutable run governors for one Job agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class AgentRunConfig:
    """Caps for one agent loop (from RuntimeConfig / host settings)."""

    max_failure_replans: int = 2
    max_iterations: int = 40
    max_subagents: int = 8
    max_subagent_depth: int = 2
    runtime_enabled: bool = True
    crew_reasoning_effort: str = "low"
    crew_reasoning_max_attempts: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_failure_replans", max(0, int(self.max_failure_replans))
        )
        object.__setattr__(self, "max_iterations", max(1, int(self.max_iterations)))
        object.__setattr__(self, "max_subagents", max(0, int(self.max_subagents)))
        object.__setattr__(
            self, "max_subagent_depth", max(1, int(self.max_subagent_depth))
        )
        effort = (self.crew_reasoning_effort or "low").strip().lower()
        if effort not in {"low", "medium", "high"}:
            effort = "low"
        object.__setattr__(self, "crew_reasoning_effort", effort)
        object.__setattr__(
            self,
            "crew_reasoning_max_attempts",
            max(1, min(5, int(self.crew_reasoning_max_attempts))),
        )


def _int(data: Mapping[str, object], key: str, default: int) -> int:
    raw = data.get(key, default)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _bool(data: Mapping[str, object], key: str, default: bool = False) -> bool:
    raw = data.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _str(data: Mapping[str, object], key: str, default: str = "") -> str:
    raw = data.get(key, default)
    if raw is None:
        return default
    return str(raw).strip() or default


def agent_run_config_from_mapping(data: Mapping[str, object]) -> AgentRunConfig:
    """Build config from a plain mapping (e.g. host settings attrs)."""
    return AgentRunConfig(
        max_failure_replans=_int(data, "AGENT_MAX_FAILURE_REPLANS", 2),
        max_iterations=_int(data, "AGENT_MAX_ITERATIONS", 40),
        max_subagents=_int(data, "AGENT_MAX_SUBAGENTS", 8),
        max_subagent_depth=_int(data, "AGENT_MAX_SUBAGENT_DEPTH", 2),
        runtime_enabled=_bool(data, "AGENT_RUNTIME_ENABLED", True),
        crew_reasoning_effort=_str(data, "CREW_REASONING_EFFORT", "low"),
        crew_reasoning_max_attempts=_int(data, "CREW_REASONING_MAX_ATTEMPTS", 1),
    )
