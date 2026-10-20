"""CrewAI project orchestration (Peon-crewAI branch).

- ``runtime_base`` — project Flow DTOs + ``CrewRuntimeBase``
- ``registry`` — ``AGENT_MODULE`` resolution (job runtime)
- ``roles`` — platform role store + affinity skill decoration + CrewAI agent factory
- ``tools`` — sandbox / RoE / findings tool allowlists
- ``runtimes`` — job role runtime + ``ProjectCrewRuntime`` Flow
"""

from orchestrator.crew.registry import (
    agent_module_id,
    get_job_runtime,
    reset_agent_modules,
)
from orchestrator.crew.roles import RoleRegistry, RoleSpec, build_crew_agent
from orchestrator.crew.runtime_base import (
    CrewRunRequest,
    CrewRunResult,
    CrewRuntimeBase,
)

__all__ = [
    "CrewRunRequest",
    "CrewRunResult",
    "CrewRuntimeBase",
    "RoleRegistry",
    "RoleSpec",
    "agent_module_id",
    "build_crew_agent",
    "get_job_runtime",
    "reset_agent_modules",
]
