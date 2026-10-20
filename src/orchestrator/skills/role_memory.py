"""Runtime role↔skill usage memory for plan/spawn digests.

Affinity sidecars are the allowlist. This table only records observed usage
so planners can prefer skills that worked before.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orchestrator.config import get_config

logger = logging.getLogger(__name__)


def load_role_skill_map() -> dict[str, dict[str, Any]]:
    from orchestrator.catalog.models import RoleSkillMemory

    out: dict[str, dict[str, Any]] = {}
    for row in RoleSkillMemory.objects.all():
        out[str(row.role_id)] = {
            "skills": list(row.skills or []),
            "counts": dict(row.counts or {}),
            "last_source": row.last_source or "",
            "last_project_id": row.last_project_id or "",
        }
    return out


def upsert_role_skills(
    role_id: str,
    skill_ids: list[str] | tuple[str, ...] | None,
    *,
    project_id: str = "",
    source: str = "runtime",
    observed: bool = True,
) -> dict[str, Any]:
    """Merge observed role skills into platform memory."""
    rid = (role_id or "").strip()
    skills = [str(s).strip() for s in (skill_ids or ()) if str(s).strip()]
    if not rid:
        return {}
    from orchestrator.catalog.models import RoleSkillMemory

    row, _created = RoleSkillMemory.objects.get_or_create(
        role_id=rid,
        defaults={
            "skills": [],
            "counts": {},
            "last_source": source,
            "last_project_id": str(project_id or ""),
        },
    )
    seen = {str(s).strip() for s in (row.skills or []) if str(s).strip()}
    for sid in skills:
        seen.add(sid)
    merged = sorted(seen)
    counts = dict(row.counts or {}) if isinstance(row.counts, dict) else {}
    if observed:
        for sid in skills:
            counts[sid] = int(counts.get(sid) or 0) + 1
    row.skills = merged
    row.counts = counts
    row.last_source = source
    if project_id:
        row.last_project_id = str(project_id)
    row.save(
        update_fields=[
            "skills",
            "counts",
            "last_source",
            "last_project_id",
            "updated_at",
        ]
    )
    entry = {
        "skills": merged,
        "counts": counts,
        "last_source": source,
        "last_project_id": str(project_id or row.last_project_id or ""),
    }

    if not observed:
        return entry

    text = (
        f"Role `{rid}` uses skills: {', '.join(merged) or '(none)'}. "
        f"source={source}"
    )
    try:
        from orchestrator.crew.memory import MemoryService

        MemoryService.remember_platform(
            text,
            category="method",
            event="role_skills",
            project_id=str(project_id or ""),
            refs=[rid, *merged[:8]],
        )
        if project_id:
            MemoryService.remember_into(
                None,
                text,
                {
                    "scope": "project",
                    "category": "method",
                    "event": "role_skills",
                    "role_id": rid,
                    "skill_ids": merged,
                    "project_id": str(project_id),
                },
            )
            try:
                from orchestrator.agent.transcript import append_jsonl, record_from_event
                from orchestrator.utils.paths import safe_workspace_key

                ws = Path(get_config().workspaces_dir) / safe_workspace_key(str(project_id))
                append_jsonl(
                    ws / "transcript.jsonl",
                    record_from_event(
                        category="method",
                        summary=text,
                        project_id=str(project_id),
                        role_id=rid,
                        event="role_skills",
                        payload={"skill_ids": merged, "source": source},
                    ),
                )
            except Exception as exc:
                logger.warning("role skill transcript append failed: %s", exc, exc_info=True)
    except Exception as exc:
        logger.warning("role_skills memory failed: %s", exc)
    return entry


def role_skills_prompt_block(*, limit: int = 24) -> str:
    """Compact observed usage for planners / routers / spawn."""
    store = load_role_skill_map()
    if not store:
        return ""
    lines = ["## Observed role → skills (runtime memory)"]
    for rid in sorted(store.keys())[: max(1, limit)]:
        row = store[rid]
        skills = row.get("skills") if isinstance(row, dict) else None
        if not isinstance(skills, list) or not skills:
            continue
        ranked = sorted(
            skills,
            key=lambda s: -int((row.get("counts") or {}).get(s) or 0),
        )
        lines.append(f"- `{rid}`: {', '.join(str(s) for s in ranked[:12])}")
    return "\n".join(lines) if len(lines) > 1 else ""
