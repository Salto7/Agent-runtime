"""CliRunner — shlex-split skill command into a Python CLI handler."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable

from context import SkillContext
from runners.runner_base import SkillRunnerBase


class CliRunner(SkillRunnerBase):
    """Shlex-split skill command and hand argv to a Python CLI handler."""

    def __init__(
        self,
        handler: Callable[[list[str]], int],
        *,
        usage: str = "",
        context: SkillContext | None = None,
    ) -> None:
        super().__init__(context)
        self.handler = handler
        self.usage = usage

    def run(self) -> int:
        raw = self.context.resolve_command()
        if not (raw or "").strip():
            print(self.usage or "usage: run.py '<subcommand and args>'", file=sys.stderr)
            return 2
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            print(f"bad command: {exc}", file=sys.stderr)
            return 2
        return int(self.handler(argv))


