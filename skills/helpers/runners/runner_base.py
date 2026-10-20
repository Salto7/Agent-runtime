"""SkillRunnerBase — one skill execution mode."""

from __future__ import annotations

from abc import ABC, abstractmethod

from context import SkillContext


class SkillRunnerBase(ABC):
    """One skill execution mode (binary / CLI / report / glue)."""

    def __init__(self, context: SkillContext | None = None) -> None:
        self.context = context or SkillContext()

    @abstractmethod
    def run(self) -> int:
        """Execute and return a process-style exit code."""

