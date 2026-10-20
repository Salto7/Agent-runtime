"""Custom shell / tool script install step."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orchestrator.tools.install_step_base import InstallStepBase
from orchestrator.tools.install_steps import util as _iu
from orchestrator.tools.install_steps.util import _under_catalog


class CustomInstallStep(InstallStepBase, step_type="custom"):
    """Priority install — runs before apt / github_release / pip / git_clone.

    YAML::

        install:
          - type: custom
            command: "curl -fsSL https://example/install.sh | bash"
          - type: apt
            packages: [fallback-pkg]

        # Or reference the tool's install script (same stem as the YAML):
        # tools/catalog/dnsx.yaml + tools/catalog/dnsx.sh
          - type: custom
            command: dnsx.sh

    ``command`` is either an inline shell line, or a relative path to the single
    allowed ``{tool_id}.sh`` beside ``{tool_id}.yaml`` (catalog-relative path).
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self.command = str(raw.get("command") or raw.get("run") or "").strip()
        try:
            self.timeout = max(30, int(raw.get("timeout") or 600))
        except (TypeError, ValueError):
            self.timeout = 600

    def apply(
        self,
        *,
        binary: str = "",
        tool_id: str = "",
        catalog_dir: Path | None = None,
    ) -> tuple[bool, str]:
        del binary
        tid = (tool_id or "").strip()
        root = catalog_dir
        if root is None:
            # circular: catalog → install_steps.custom
            from orchestrator.tools.catalog.catalog import ToolCatalog

            root = ToolCatalog.shared().catalog_dir()
        payload, label = self._resolve_payload(tid, Path(root))
        if not payload:
            return False, label or "empty custom install"
        code, out, err = _iu._run(["bash", "-lc", payload], timeout=self.timeout)
        if code:
            detail = (err or out).strip()[:500]
            return False, f"custom install: {detail or f'exit {code}'}"
        return True, label

    def _resolve_payload(self, tool_id: str, catalog_dir: Path) -> tuple[str, str]:
        """Inline shell, or the tool's ``{id}.sh`` under the catalog dir."""
        raw = self.command
        script_name = f"{tool_id}.sh" if tool_id else ""
        script_path = (catalog_dir / script_name).resolve() if script_name else None

        def _is_script_ref(text: str) -> bool:
            if not script_name or re.search(r"\s", text.strip()):
                return False   # an inline command (``bash x.sh``, ``curl … | sh``) is not a bare script reference
            norm = text.replace("\\", "/").removeprefix("./")
            return (
                norm == script_name
                or norm.endswith(f"/{script_name}")
                or Path(norm).name == script_name
            )

        if script_path is not None and (not raw or _is_script_ref(raw)):
            if script_path.is_file() and _under_catalog(catalog_dir, script_path):
                try:
                    body = script_path.read_text(encoding="utf-8")
                except OSError as exc:
                    return "", f"cannot read {script_name}: {exc}"
                if not body.strip():
                    return "", f"empty install script {script_name}"
                return body, f"custom:script:{script_name}"
            if raw:
                return "", f"missing install script {script_name} (beside {tool_id}.yaml)"
            return "", f"empty custom install (no command and no {tool_id}.sh)"

        if not raw:
            return "", "empty custom install"
        if not re.search(r"\s", raw) and raw.endswith(".sh") and ("/" in raw or "\\" in raw or Path(raw).name != raw):
            return (
                "",
                f"custom script must be {script_name or '{tool_id}.sh'} beside the tool YAML",
            )
        return raw, "custom:inline"
