"""Host-CLI base for runtime wrappers (Docker / OpenShell)."""

from __future__ import annotations

import shutil

from agent_runtime.api import ExecResult
from agent_runtime.process import HostProcessRunner
from agent_runtime.util import SharedBase


class CLIBase(SharedBase):
    """``bin`` / ``require_bin`` / ``run`` for a host CLI tool."""

    binary_name: str = ""
    missing_error: str = ""

    def bin(self) -> str | None:
        return shutil.which(self.binary_name) if self.binary_name else None

    def available(self) -> bool:
        return bool(self.bin())

    def require_bin(self) -> str:
        path = self.bin()
        if not path:
            raise RuntimeError(
                self.missing_error or f"{self.binary_name or 'CLI'} missing"
            )
        return path

    def run(self, args: list[str], *, timeout: float = 120) -> ExecResult:
        return HostProcessRunner.run([self.require_bin(), *args], timeout=timeout)
