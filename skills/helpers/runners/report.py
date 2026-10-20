"""ReportRunner — build findings/report.md via a workspace builder."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from context import SkillContext
from runners.runner_base import SkillRunnerBase
from workspace import WorkspaceStore


class ReportRunner(SkillRunnerBase):
    """Build findings/report.md via a workspace builder callback."""

    def __init__(
        self,
        builder: Callable[[Path], str | Path],
        *,
        context: SkillContext | None = None,
        store: WorkspaceStore | None = None,
    ) -> None:
        super().__init__(context)
        self.builder = builder
        self.store = store or WorkspaceStore(self.context)

    def run(self) -> int:
        ws = self.store.path()
        findings = ws / "findings"
        findings.mkdir(parents=True, exist_ok=True)
        result = self.builder(ws)
        if isinstance(result, Path):
            print(f"Wrote {result}")
        else:
            report = findings / "report.md"
            report.write_text(str(result), encoding="utf-8")
            print(f"Wrote {report}")
        return 0


