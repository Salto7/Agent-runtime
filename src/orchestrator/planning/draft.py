"""LLM plan drafting (no persistence): RoleRouter + Job/Project planners.

Hosts persist the draft their own way; ``Orchestrator.plan`` / ``run_project``
turn a project draft into in-memory objectives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from orchestrator.crew.roles.catalog import resolve_role_id
from orchestrator.crew.roles.registry import RoleRegistry
from orchestrator.crew.router import RoleRouter
from orchestrator.planning import (
    JobPlanner,
    ProjectPlanner,
    bookend_role_ids,
    parse_project_objectives,
)
from orchestrator.skills.role_memory import role_skills_prompt_block
from orchestrator.utils import llm_usage
from orchestrator.utils.llm import chat_messages


@dataclass
class PlanDraft:
    """LLM plan output before the host stores it."""

    mode: str
    plan_text: str
    role_ids: list[str] = field(default_factory=list)
    objectives_payload: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""
    title: str = "adhoc"
    summary: str = ""
    in_scope: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    authorization: str = ""
    preferred_tags: list[str] = field(default_factory=list)


def validate_project_objectives(objectives_payload: list[dict[str, Any]]) -> None:
    """Reject plans that cannot create a runnable objective graph."""
    registry = RoleRegistry.shared()
    roles = registry.list_roles()
    if not roles:
        raise ValueError("No roles found — check the role catalog")
    manager = registry.manager()
    analyzer = registry.analyzer()
    if manager is None or analyzer is None:
        missing = []
        if manager is None:
            missing.append("manager role (kind engagement-pm)")
        if analyzer is None:
            missing.append("reporting bookend (kind analyzer)")
        raise ValueError("role catalog is missing: " + ", ".join(missing))
    rows = [row for row in objectives_payload if isinstance(row, dict)]
    if not rows:
        raise ValueError("Planner returned no objectives")
    known = {role.id for role in roles}
    invalid: list[str] = []
    for index, row in enumerate(rows, start=1):
        if not str(row.get("title") or "").strip():
            raise ValueError(f"Planner objective {index} has no title")
        role_id = resolve_role_id(str(row.get("role_id") or "").strip())
        if not role_id:
            raise ValueError(f"Planner objective {index} has no role_id")
        if role_id not in known and role_id not in invalid:
            invalid.append(role_id)
    if invalid:
        raise ValueError("Planner used unknown role id(s): " + ", ".join(invalid))
    if resolve_role_id(str(rows[0].get("role_id") or "").strip()) != manager.id:
        raise ValueError(f"First objective must use manager role {manager.id}")
    if resolve_role_id(str(rows[-1].get("role_id") or "").strip()) != analyzer.id:
        raise ValueError(f"Last objective must use analyzer role {analyzer.id}")


def draft_llm_plan(
    *,
    description: str,
    mode: str = "project",
    title: str = "adhoc",
    summary: str = "",
    in_scope: Iterable[str] = (),
    exclusions: Iterable[str] = (),
    authorization: str = "",
    role_ids: list[str] | None = None,
    preferred_tags: list[str] | None = None,
    prior_plan: str = "",
    existing_objectives: Iterable[str] = (),
) -> PlanDraft:
    """Call RoleRouter + planners; return the draft."""
    text = (description or "").strip()
    if not text:
        raise ValueError("description is required")
    mode_norm = (mode or "project").strip().lower()
    if mode_norm not in {"project", "job"}:
        raise ValueError("mode must be 'project' or 'job'")

    scope, excl = list(in_scope), list(exclusions)
    tags = list(preferred_tags or [])
    resolved = RoleRouter.shared().resolve(
        text, explicit=list(role_ids or []) or None, project=mode_norm == "project"
    )
    prompt = f"{text}\n\nPreloaded roles: {', '.join(resolved)}" if resolved else text

    if mode_norm == "project":
        planner = ProjectPlanner()
        learned = role_skills_prompt_block()
        msgs = planner.build_messages(
            prompt,
            project_title=title,
            project_summary=summary,
            roe=SimpleNamespace(
                in_scope="\n".join(scope),
                exclusions="\n".join(excl),
                authorization_note=authorization or "",
                testing_window_notes="",
                abort_triggers="",
            ),
            filtered_roles_index=RoleRouter.shared().roles_index_text()
            + (("\n\n" + learned) if learned else ""),
            focus_tags=tags or None,
            prior_plan=prior_plan,
            existing_objectives_summary="\n".join(existing_objectives),
        )
        with llm_usage.purpose("planner_project"):
            raw = planner.message_text(chat_messages(msgs))
        payload = parse_project_objectives(raw)
        obj_roles = [
            str(o.get("role_id") or "").strip()
            for o in (payload.get("objectives") or [])
            if str(o.get("role_id") or "").strip()
        ]
        return PlanDraft(
            mode=mode_norm,
            plan_text=planner.format_result(raw, project_title=title),
            role_ids=bookend_role_ids([*obj_roles, *resolved]),
            objectives_payload=list(payload.get("objectives") or []),
            description=description,
            title=title,
            summary=summary,
            in_scope=scope,
            exclusions=excl,
            authorization=authorization or "",
            preferred_tags=tags,
        )

    planner = JobPlanner()
    with llm_usage.purpose("planner_job"):
        plan_text = planner.format_result(
            planner.message_text(chat_messages(planner.build_messages(prompt)))
        )
    return PlanDraft(
        mode=mode_norm,
        plan_text=plan_text,
        role_ids=resolved,
        description=description,
        title=title,
        preferred_tags=tags,
    )


def read_prior_plan(workspace: Path) -> str:
    """Last saved plan for a project workspace (``plans/latest.md``)."""
    path = Path(workspace) / "plans" / "latest.md"
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:4000] if path.is_file() else ""
    except OSError:
        return ""
