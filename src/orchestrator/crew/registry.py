"""Resolve agent / crew modules from RuntimeConfig (parallel to llm.registry)."""

from __future__ import annotations

import importlib

from orchestrator.agent.runtime_base import AgentRuntimeBase
from orchestrator.config import get_config

# Job-level modules (one Job / role tool loop).
_JOB_MODULES: dict[str, str] = {
    "crewai": "orchestrator.crew.runtimes.job_crewai:CrewAIJobRuntime",
}

_job_cached: AgentRuntimeBase | None = None
_job_cached_id: str | None = None


def reset_agent_modules() -> None:
    """Drop cached runtimes (call after ``configure()``)."""
    global _job_cached, _job_cached_id
    _job_cached = None
    _job_cached_id = None


def _load_class(path: str) -> type:
    module_path, _, cls_name = path.partition(":")
    if not module_path or not cls_name:
        raise RuntimeError(f"invalid module path: {path!r}")
    mod = importlib.import_module(module_path)
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise RuntimeError(f"module {module_path!r} has no {cls_name}")
    return cls


def agent_module_id() -> str:
    """Return configured module id (crewai only)."""
    return (get_config().agent_module or "crewai").strip().lower() or "crewai"

def get_job_runtime() -> AgentRuntimeBase:
    """Return the Job-level agent runtime for ``AGENT_MODULE``."""
    global _job_cached, _job_cached_id
    module_id = agent_module_id()
    if _job_cached is not None and _job_cached_id == module_id:
        return _job_cached
    path = _JOB_MODULES.get(module_id)
    if path is None:
        known = ", ".join(sorted(_JOB_MODULES))
        raise RuntimeError(
            f"Unknown AGENT_MODULE={module_id!r}; known modules: {known}"
        )
    _job_cached = _load_class(path)()
    _job_cached_id = module_id
    return _job_cached
