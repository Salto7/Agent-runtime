"""Orchestrator — a stand-alone agent runtime (CrewAI + LiteLLM + Docker sandbox, no Django).

Package map:
- ``api`` — :class:`Orchestrator` facade (plan / run / run_project / dream)
- ``host`` — in-process control plane + per-project sandbox (replaces Peon's Django layer)
- ``crew`` — CrewAI role + project-crew runtimes, tools, Cognee memory, dreaming
- ``agent`` — job scope / bridge contract / delegation / runtime ABC
- ``brain`` — A2A messaging hub
- ``catalog`` / ``memory`` — SQLite-backed platform catalog and agent memory
- ``llm`` — LiteLLM provider + optional OpenAI-compatible proxy lifecycle
- ``skills`` / ``tools`` / ``mcp`` / ``learn`` / ``marketplace`` — skills, CLI catalog, MCP, authoring
- ``planning`` — job / project planners
- ``utils`` — shared helpers (llm, usage telemetry, stream, paths, …)

``agent_runtime`` (sibling package) holds the Docker / OpenShell execution backends.
"""

from orchestrator.api import Orchestrator, OrchestratorResult, PlanResult, run
from orchestrator.config import RuntimeConfig, bootstrap, configure, discover_root, get_config

__all__ = [
    "Orchestrator",
    "OrchestratorResult",
    "PlanResult",
    "RuntimeConfig",
    "bootstrap",
    "configure",
    "discover_root",
    "get_config",
    "run",
]

__version__ = "0.2.0"
