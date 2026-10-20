"""Planner ABC: message normalize, context blocks, persist."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.utils.strings import plain_text
from orchestrator.utils.paths import (
    ensure_workspace_layout,
    provision_job_workspace,
    safe_workspace_key,
)


class PlannerBase(ABC):
    """Common plumbing for job and project planners."""

    plan_title: str = "Plan"

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """Planner system prompt for this mode."""

    def message_text(self, content: Any) -> str:
        """Normalize LLM message content to plain text."""
        return plain_text(content)

    def append_context(
        self,
        parts: list[str],
        *,
        prior_plan: str = "",
        transcript_digest: str = "",
    ) -> None:
        """Append shared optional context sections to the human message parts."""
        digest = (transcript_digest or "").strip()
        if digest:
            parts.append(f"Engagement transcript digest:\n{digest}")
        if prior_plan.strip():
            parts.append(f"Previous plan (revise if needed):\n{prior_plan.strip()}")

    def wrap_messages(self, human_parts: list[str]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": "\n\n".join(human_parts)},
        ]

    def persist(
        self,
        job_id: str,
        plan_text: str,
        *,
        reason: str = "",
        root: Path | None = None,
    ) -> str:
        """Save plan under job workspace plans/. Returns relative path."""
        if not job_id or not (plan_text or "").strip():
            return ""
        try:
            if root is None:
                workspace = provision_job_workspace(job_id)
            else:
                base = Path(root).resolve()
                base.mkdir(parents=True, exist_ok=True)
                workspace = (base / safe_workspace_key(job_id)).resolve()
                workspace.relative_to(base)
                ensure_workspace_layout(workspace)
            plan_dir = workspace / "plans"
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            body = self.message_text(plan_text).strip() or (plan_text or "").strip()
            md = (
                f"# {self.plan_title}\n\n"
                f"> Saved by planner ({reason or 'initial'}).\n\n"
                f"{body}\n"
            )
            dest = plan_dir / f"{stamp}-plan.md"
            dest.write_text(md, encoding="utf-8")
            (plan_dir / "latest.md").write_text(md, encoding="utf-8")
            return f"plans/{dest.name}"
        except Exception:
            return ""

    @abstractmethod
    def build_messages(self, description: str, **kwargs) -> list[dict[str, str]]:
        """Build chat messages for the planner LLM."""

    @abstractmethod
    def format_result(self, raw_llm_text: str, **kwargs) -> str:
        """Turn raw LLM output into the final plan string."""
