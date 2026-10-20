"""Project-level CrewAI runtime ABC (one Project = one Flow).

Job-level tool loops stay on ``orchestrator.agent.runtime_base.AgentRuntimeBase``.
This module owns hierarchical role orchestration for a whole engagement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CrewRunResult:
    ok: bool
    output: str = ""
    error: str = ""
    flow_id: str = ""
    status: str = ""  # running | paused | awaiting_feedback | stopped | done
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrewRunRequest:
    """Start / resume / replan one project crew."""

    project_id: str
    brief: str = ""
    role_ids: tuple[str, ...] = ()
    resume: bool = False
    replan: bool = False
    steer: str = ""
    flow_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class CrewRuntimeBase(ABC):
    """Framework-agnostic project crew execution."""

    @abstractmethod
    def start(self, request: CrewRunRequest) -> CrewRunResult:
        """Kick off (or continue) the project Flow."""

    def resume(self, request: CrewRunRequest) -> CrewRunResult:
        return self.start(
            CrewRunRequest(
                project_id=request.project_id,
                brief=request.brief,
                role_ids=request.role_ids,
                resume=True,
                replan=request.replan,
                steer=request.steer,
                flow_id=request.flow_id,
                metadata=request.metadata,
            )
        )

    def replan(self, request: CrewRunRequest) -> CrewRunResult:
        return self.start(
            CrewRunRequest(
                project_id=request.project_id,
                brief=request.brief,
                role_ids=request.role_ids,
                resume=True,
                replan=True,
                steer=request.steer,
                flow_id=request.flow_id,
                metadata=request.metadata,
            )
        )
