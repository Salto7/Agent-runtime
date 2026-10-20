"""BinaryRunner — run a native CLI once per resolved command."""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from binary import NativeBinaryResolver
from context import SkillContext
from runners.runner_base import SkillRunnerBase
from workspace import WorkspaceStore

try:
    from orchestrator_tools import stream
except ImportError:  # pragma: no cover

    def stream(message_type: str, content: str, **metadata):  # type: ignore[misc]
        del message_type, metadata
        if content:
            print(content, flush=True)


class BinaryRunner(SkillRunnerBase):
    """Run a native CLI once per resolved command (all in-scope targets by default)."""

    def __init__(
        self,
        binary: str,
        *,
        default_for_target: Callable[[str], str] | None = None,
        timeout: int = 300,
        context: SkillContext | None = None,
        resolver: NativeBinaryResolver | None = None,
        store: WorkspaceStore | None = None,
    ) -> None:
        super().__init__(context)
        self.binary = binary
        self.default_for_target = default_for_target
        self.timeout = timeout
        self.resolver = resolver or NativeBinaryResolver()
        self.store = store or WorkspaceStore(self.context)

    def run(self) -> int:
        commands = self.context.resolve_commands(
            default_for_target=self.default_for_target
        )
        if not commands:
            print(
                f"usage: run.py '<{self.binary} …>'  "
                "(or pass argv / set ORCHESTRATOR_IN_SCOPE)",
                file=sys.stderr,
            )
            return 2
        resolved = self.resolver.resolve(self.binary)
        if not resolved:
            msg = (
                f"{self.binary} not on PATH (or only a Python wrapper was found) — "
                "provision via tools/catalog before running this skill"
            )
            stream("error", msg)
            print(msg, file=sys.stderr)
            return 1

        out_chunks: list[str] = []
        err_chunks: list[str] = []
        argv_log: list[str] = []
        worst = 0
        for cmd in commands:
            try:
                tokens = shlex.split(cmd)
            except ValueError as exc:
                print(f"bad command: {exc}", file=sys.stderr)
                return 2
            if not tokens:
                print(f"empty {self.binary} command", file=sys.stderr)
                return 2
            # SKILL.md documents ``args`` as the binary's own arguments; a full
            # command line that starts with the binary name is accepted too.
            # Only the resolved binary is ever executed either way.
            rest = tokens[1:] if Path(tokens[0]).name == self.binary else tokens
            argv = [resolved, *rest]
            argv_log.append(" ".join(argv))
            stream("log", f"running: {' '.join(argv)}")
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=self.timeout
                )
            except subprocess.TimeoutExpired:
                stream("error", f"{self.binary} timed out")
                print(f"{self.binary} timed out", file=sys.stderr)
                return 124
            code = proc.returncode or 0
            if proc.stdout:
                stream("stdout", proc.stdout.rstrip("\n"), exit_code=code)
                print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
                out_chunks.append(proc.stdout.rstrip("\n"))
            if proc.stderr:
                stream("stderr", proc.stderr.rstrip("\n"), exit_code=code)
                print(
                    proc.stderr,
                    end="" if proc.stderr.endswith("\n") else "\n",
                    file=sys.stderr,
                )
                err_chunks.append(proc.stderr.rstrip("\n"))
            if code and (worst == 0 or code > worst):
                worst = code

        skill = self.context.skill_name()
        if skill and skill != "analyzer":
            self.store.persist_skill_run(
                skill=skill,
                binary=self.binary,
                argv=argv_log,
                stdout="\n".join(out_chunks),
                stderr="\n".join(err_chunks),
                code=worst,
            )
            # Do NOT record_finding here — run completion is ops status, not an
            # engagement finding. Skills/agents call record_finding for real discoveries.
        return worst


