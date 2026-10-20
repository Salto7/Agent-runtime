"""GitHub release binary install step."""
from __future__ import annotations

import shlex
from typing import Any

from orchestrator.tools.install_step_base import InstallStepBase
from orchestrator.tools.install_steps import util as _iu
from orchestrator.tools.install_steps.util import _GH_SCRIPT, _REPO_RE


class GitHubReleaseInstallStep(InstallStepBase, step_type="github_release"):
    def __init__(self, raw: dict[str, Any]) -> None:
        self.repo = str(raw.get("repo") or "").strip()
        self.binary = str(raw.get("binary") or "").strip()
        self.asset_substr = str(raw.get("asset_substr") or "linux_amd64").strip()
        self.tag = str(raw.get("tag") or "").strip()

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del tool_id
        match = _REPO_RE.match(self.repo)
        if not match:
            return False, f"invalid github repo {self.repo!r}"
        owner, name = match.group(1), match.group(2)
        bin_name = (self.binary or binary or name).strip()
        if not bin_name:
            return False, "binary name required"
        if not _GH_SCRIPT.is_file():
            return False, f"missing install script {_GH_SCRIPT}"
        script = _GH_SCRIPT.read_text(encoding="utf-8")
        for key, val in {
            "__OWNER__": shlex.quote(owner),
            "__REPO__": shlex.quote(name),
            "__BINARY__": shlex.quote(bin_name),
            "__TAG__": shlex.quote(self.tag),
            "__ASSET_SUBSTR__": shlex.quote(self.asset_substr or "linux_amd64"),
        }.items():
            script = script.replace(key, val)
        code, out, err = _iu._run(["bash", "-lc", script], timeout=600)
        if code:
            return False, f"github {self.repo}/{bin_name}: {(err or out).strip()[:500]}"
        return True, f"github:{bin_name}"
