"""Git clone install step."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from orchestrator.tools.install_step_base import InstallStepBase
from orchestrator.tools.install_steps.apt import AptInstallStep
from orchestrator.tools.install_steps import util as _iu
from orchestrator.tools.install_steps.util import _REPO_RE


class GitCloneInstallStep(InstallStepBase, step_type="git_clone"):
    """Shallow-clone a GitHub repo and symlink an entrypoint onto PATH."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.repo = str(raw.get("repo") or "").strip()
        self.ref = str(raw.get("ref") or "").strip()
        self.entrypoint = str(raw.get("entrypoint") or "").strip()
        self.binary = str(raw.get("binary") or "").strip()
        try:
            self.depth = max(1, int(raw.get("depth") or 1))
        except (TypeError, ValueError):
            self.depth = 1

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del tool_id
        match = _REPO_RE.match(self.repo)
        if not match:
            return False, f"invalid github repo {self.repo!r}"
        owner, name = match.group(1), match.group(2)
        entry = (self.entrypoint or "").strip().lstrip("/")
        if not entry or entry.startswith("..") or "/../" in f"/{entry}/":
            return False, "entrypoint required (repo-relative path)"
        bin_name = (self.binary or binary or Path(entry).stem).strip()
        if not bin_name:
            return False, "binary name required"
        dest = Path("/opt/catalog-tools") / name
        url = f"https://github.com/{owner}/{name}.git"
        entry_path = dest / entry
        bin_path = f"/usr/local/bin/{bin_name}"
        AptInstallStep({"packages": ["git"]}).apply()
        script = f"""
set -euo pipefail
mkdir -p /opt/catalog-tools
rm -rf {shlex.quote(str(dest))}
git clone --depth {self.depth} {shlex.quote(url)} {shlex.quote(str(dest))}
"""
        if self.ref:
            script += f"git -C {shlex.quote(str(dest))} fetch --depth {self.depth} origin {shlex.quote(self.ref)}\n"
            script += f"git -C {shlex.quote(str(dest))} checkout {shlex.quote(self.ref)}\n"
        script += f"""
ENTRY={shlex.quote(str(entry_path))}
test -f "$ENTRY"
chmod +x "$ENTRY" || true
ln -sfn "$ENTRY" {shlex.quote(bin_path)}
"""
        code, out, err = _iu._run(["bash", "-lc", script], timeout=600)
        if code:
            return False, f"git_clone {self.repo}: {(err or out).strip()[:500]}"
        return True, f"git_clone:{bin_name}"
