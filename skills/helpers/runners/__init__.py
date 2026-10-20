"""Skill runners — class hierarchy behind thin ``scripts/run.py`` entrypoints."""

from __future__ import annotations

from collections.abc import Callable

from runners.binary import BinaryRunner
from runners.cli import CliRunner
from runners.report import ReportRunner
from runners.runner_base import SkillRunnerBase

__all__ = [
    "BinaryRunner",
    "CliRunner",
    "ReportRunner",
    "SkillRunnerBase",
    "main",
]


def main(fn: Callable[[], int]) -> None:
    """``if __name__ == "__main__": main(_run)``."""
    raise SystemExit(fn())
