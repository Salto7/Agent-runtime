"""Apt package install step."""
from __future__ import annotations

from typing import Any

from orchestrator.tools.install_step_base import InstallStepBase
from orchestrator.tools.install_steps import util as _iu
from orchestrator.tools.install_steps.util import _ensure_env_command_aliases, _pkgs


class AptInstallStep(InstallStepBase, step_type="apt"):
    def __init__(self, raw: dict[str, Any]) -> None:
        self.packages = _pkgs(raw)

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del binary, tool_id
        if not self.packages:
            return True, "no apt packages"
        # PATH skip lives in InstallResolver / CatalogProvisioner._verified.
        # Here only skip packages dpkg already has (Kali metapackages).
        missing = [pkg for pkg in self.packages if not _iu._apt_package_installed(pkg)]
        if not missing:
            _ensure_env_command_aliases(self.packages)
            return True, f"apt:already:{','.join(self.packages)}"
        _iu._run(["apt-get", "update", "-qq"], timeout=180)
        code, out, err = _iu._run(
            ["apt-get", "install", "-y", "--no-install-recommends", *missing],
            timeout=600,
        )
        if code:
            return False, f"apt install {missing}: {(err or out).strip()[:400]}"
        _ensure_env_command_aliases(self.packages)
        return True, f"apt:{','.join(missing)}"
