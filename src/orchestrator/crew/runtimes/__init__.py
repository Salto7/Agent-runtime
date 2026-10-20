"""Concrete crew / job runtimes (lazy where heavy)."""

from __future__ import annotations

from typing import Any

__all__ = ["CrewAIJobRuntime", "ProjectCrewRuntime"]


def __getattr__(name: str) -> Any:
    if name == "CrewAIJobRuntime":
        # deferred: optional heavy crewai runtime
        from orchestrator.crew.runtimes.job_crewai import CrewAIJobRuntime

        return CrewAIJobRuntime
    if name == "ProjectCrewRuntime":
        # deferred: optional heavy crewai runtime
        from orchestrator.crew.runtimes.project_crewai import ProjectCrewRuntime

        return ProjectCrewRuntime
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
