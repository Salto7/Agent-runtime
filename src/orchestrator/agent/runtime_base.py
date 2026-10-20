"""Agent runtime ABC and run DTOs (one Job = one tool loop)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from orchestrator.agent.config import AgentRunConfig
from orchestrator.agent.job import JobScope


@dataclass(frozen=True)
class AgentRunResult:
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    replans: int = 0


@dataclass(frozen=True)
class AgentRunRequest:
    """Input for start/resume of one Job agent run."""

    scope: JobScope
    config: AgentRunConfig
    resume: bool = False
    steer: str = ""


class AgentRuntimeBase(ABC):
    """Framework-agnostic Job agent execution."""

    @abstractmethod
    def start(self, request: AgentRunRequest) -> AgentRunResult:
        """Run (or continue) the agent for ``request.scope.job_id``."""

    def resume(self, request: AgentRunRequest) -> AgentRunResult:
        return self.start(
            AgentRunRequest(
                scope=request.scope,
                config=request.config,
                resume=True,
                steer=request.steer,
            )
        )


_DEFAULT: AgentRuntimeBase | None = None


def get_agent_runtime() -> AgentRuntimeBase:
    """Return Job runtime for ``AGENT_MODULE`` (crewai)."""
    global _DEFAULT
    if _DEFAULT is None:
        # circular: crew.registry → AgentRuntimeBase
        from orchestrator.crew.registry import get_job_runtime

        _DEFAULT = get_job_runtime()
    return _DEFAULT


def set_agent_runtime(runtime: AgentRuntimeBase | None) -> None:
    global _DEFAULT
    _DEFAULT = runtime
    if runtime is None:
        # circular: crew.registry → AgentRuntimeBase
        from orchestrator.crew.registry import reset_agent_modules

        reset_agent_modules()


def run_agent(
    scope: JobScope,
    config: AgentRunConfig,
    *,
    resume: bool = False,
    steer: str = "",
) -> AgentRunResult:
    """Control-plane entry: run one Job via the process agent runtime."""
    return get_agent_runtime().start(
        AgentRunRequest(scope=scope, config=config, resume=resume, steer=steer)
    )
