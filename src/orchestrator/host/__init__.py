"""Stand-alone host layer: the in-process replacement for Peon's Django control plane.

- ``control`` — :class:`LocalControlPlane` (jobs, threads, A2A wake) and
  :class:`LocalAgentBridge` (the agent's view of spawn / messaging / memory / findings)
- ``sandbox`` — :class:`ProjectSandbox` (per-project Docker/OpenShell runtime)
"""

from orchestrator.host.control import LocalAgentBridge, LocalControlPlane, ThreadExecutor
from orchestrator.host.state import (
    JobState,
    MemoryStateStore,
    Objective,
    ProjectMeta,
    Roe,
    StateStore,
)

__all__ = [
    "JobState",
    "LocalAgentBridge",
    "LocalControlPlane",
    "MemoryStateStore",
    "Objective",
    "ProjectMeta",
    "Roe",
    "StateStore",
    "ThreadExecutor",
]
