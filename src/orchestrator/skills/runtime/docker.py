"""Skill script runner. Argv only; delegates to ``SandboxTools.script``."""

from __future__ import annotations

from typing import Sequence

from orchestrator.skills.runtime.bodies import run_skill_script_body


class DockerSkillRuntime:
    """Compatibility wrapper. Prefer ``SandboxTools.script`` with argv."""

    def run(
        self,
        skill_id: str,
        relative_entry: str,
        args: Sequence[str] | None = None,
    ) -> str:
        return run_skill_script_body(skill_id, relative_entry, list(args or ()))
