"""Per-Job scope bag and ContextVar binding for the active agent run."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from orchestrator.agent.bridge_base import AgentBridgeBase
from orchestrator.agent.bridges import NullAgentBridge
from orchestrator.agent.config import AgentRunConfig


@dataclass
class JobScope:
    """Everything one Job agent needs (injected by the control plane)."""

    job_id: str
    project_id: str = ""
    parent_job_id: str = ""
    workspace: str = ""
    role_ids: list[str] = field(default_factory=list)
    brief: str = ""
    depth: int = 1
    bridge: AgentBridgeBase = field(default_factory=NullAgentBridge)
    extras: dict[str, Any] = field(default_factory=dict)


_scope: ContextVar[JobScope | None] = ContextVar("agent_job_scope", default=None)
_cfg: ContextVar[AgentRunConfig | None] = ContextVar("agent_job_cfg", default=None)


def get_job() -> JobScope:
    scope = _scope.get()
    if scope is None:
        raise RuntimeError("no agent job scope bound")
    return scope


def get_agent_config() -> AgentRunConfig:
    """Bound ``AgentRunConfig`` for the current job (not ``orchestrator.config``)."""
    cfg = _cfg.get()
    if cfg is None:
        return AgentRunConfig()
    return cfg


@contextmanager
def bind_job(scope: JobScope, config: AgentRunConfig) -> Iterator[None]:
    t_scope = _scope.set(scope)
    t_cfg = _cfg.set(config)
    try:
        yield
    finally:
        _scope.reset(t_scope)
        _cfg.reset(t_cfg)
