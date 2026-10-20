"""Concrete InstallStepBase implementations (register via step_type)."""
from __future__ import annotations

from typing import Any

from orchestrator.tools.install_step_base import InstallStepBase
from orchestrator.tools.install_steps.apt import AptInstallStep
from orchestrator.tools.install_steps.custom import CustomInstallStep
from orchestrator.tools.install_steps.git_clone import GitCloneInstallStep
from orchestrator.tools.install_steps.github_release import GitHubReleaseInstallStep
from orchestrator.tools.install_steps.pip import PipInstallStep

__all__ = [
    "AptInstallStep",
    "CustomInstallStep",
    "GitCloneInstallStep",
    "GitHubReleaseInstallStep",
    "PipInstallStep",
    "_fallback_rank",
    "_steps",
]


def _steps(raw_steps: list[Any]) -> list[InstallStepBase]:
    out: list[InstallStepBase] = []
    for raw in raw_steps or []:
        if isinstance(raw, dict):
            step = InstallStepBase.from_dict(raw)
            if step:
                out.append(step)
    return out


def _fallback_rank(step: InstallStepBase) -> int:
    """After custom: apt (batched) → github_release → pip → git_clone."""
    if isinstance(step, GitHubReleaseInstallStep):
        return 0
    if isinstance(step, PipInstallStep):
        return 1
    if isinstance(step, GitCloneInstallStep):
        return 2
    return 9
