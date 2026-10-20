"""Pip package install step."""
from __future__ import annotations

from typing import Any

from orchestrator.tools.install_step_base import InstallStepBase
from orchestrator.tools.install_steps import util as _iu
from orchestrator.tools.install_steps.util import _pkgs


class PipInstallStep(InstallStepBase, step_type="pip"):
    def __init__(self, raw: dict[str, Any]) -> None:
        self.packages = _pkgs(raw)

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del binary, tool_id
        if not self.packages:
            return True, "no pip packages"
        code, out, err = _iu._run(
            ["python3", "-m", "pip", "install", "--break-system-packages", *self.packages],
            timeout=600,
        )
        if code:
            return False, f"pip {self.packages}: {(err or out).strip()[:300]}"
        return True, f"pip:{' '.join(self.packages)}"
