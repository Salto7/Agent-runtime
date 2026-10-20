"""Shared multi-agent delegation bodies (Crew + capability adapters call these).

Spawn policy is agent-driven: catalogs are the armory; the bridge enforces
existence, deny-classes, caps, and RoE — not per-role YAML allowlists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from orchestrator.agent.job import get_agent_config, get_job


def _parse_skill_ids(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize skill id list from tool args (CSV or JSON array string)."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(s).strip() for s in parsed if str(s).strip()]
    return [p.strip() for p in text.split(",") if p.strip()]


@dataclass(frozen=True)
class SpawnWorkRequest:
    """Structured spawn request (control plane + stream payload)."""

    title: str
    brief: str
    role_id: str = ""
    link: str = "child"
    scope_slice: dict[str, str] | None = None
    idempotency_key: str = ""
    skill_ids: tuple[str, ...] = field(default_factory=tuple)

    def role_ids(self) -> list[str] | None:
        rid = (self.role_id or "").strip()
        return [rid] if rid else None

    def payload(self, *, job_id: str = "", parent_job_id: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {
            "title": self.title,
            "brief": (self.brief or "")[:500],
            "role_id": (self.role_id or "").strip(),
            "link": self.link,
            "job_id": job_id,
            "parent_job_id": parent_job_id,
            "skill_ids": list(self.skill_ids),
        }
        if self.scope_slice:
            out["scope_slice"] = dict(self.scope_slice)
        if self.idempotency_key:
            out["idempotency_key"] = self.idempotency_key
        return out


def spawn_work_body(
    title: str,
    description: str,
    role_id: str = "",
    link: str = "child",
    scope_slice_json: str = "",
    skill_ids: str = "",
) -> str:
    """Spawn a child (default) or peer Job via the bound bridge."""
    cfg = get_agent_config()
    ctx = get_job()
    kind = (link or "child").strip().lower() or "child"
    if kind not in {"peer", "child"}:
        return "Error: link must be 'peer' or 'child'"
    if kind == "child" and ctx.depth >= cfg.max_subagent_depth:
        return f"Error: child depth limit ({cfg.max_subagent_depth})"

    scope: dict[str, str] | None = None
    raw_slice = (scope_slice_json or "").strip()
    if raw_slice:
        try:
            parsed = json.loads(raw_slice)
        except json.JSONDecodeError as exc:
            return f"Error: scope_slice must be JSON object: {exc}"
        if not isinstance(parsed, dict):
            return "Error: scope_slice must be a JSON object"
        scope = {
            str(k): str(v)
            for k, v in parsed.items()
            if str(k).strip() and str(v).strip()
        }

    skills = tuple(_parse_skill_ids(skill_ids))
    req = SpawnWorkRequest(
        title=(title or "").strip() or "child-agent",
        brief=(description or "").strip(),
        role_id=(role_id or "").strip(),
        link=kind,
        scope_slice=scope or None,
        skill_ids=skills,
    )
    try:
        job_id = ctx.bridge.spawn_agent(
            title=req.title,
            description=req.brief,
            role_ids=req.role_ids(),
            link=kind,  # type: ignore[arg-type]
            scope_slice=req.scope_slice,
            skill_ids=list(req.skill_ids) or None,
        )
    except Exception as exc:
        return f"Error spawning agent: {exc}"

    if isinstance(job_id, str) and job_id.startswith("Error"):
        return job_id
    # Bridge owns stream envelope + transcript; return a short ack only.
    return f"spawned {kind} job {job_id}"


def wait_for_work_body(timeout_seconds: int = 600, job_ids: str = "") -> str:
    """Non-blocking status of child agents this job spawned."""
    ids = [j.strip() for j in (job_ids or "").split(",") if j.strip()] or None
    try:
        return get_job().bridge.wait_agents(
            job_ids=ids, timeout_seconds=int(timeout_seconds)
        )
    except Exception as exc:
        return f"Error waiting for work: {exc}"


def list_work_body() -> str:
    """List other Jobs on the same objective."""
    try:
        return get_job().bridge.list_agents()
    except Exception as exc:
        return f"Error listing work: {exc}"


def list_roles_body() -> str:
    """Browse engagement role catalog (armory for spawn_work)."""
    from orchestrator.crew.router import RoleRouter

    return RoleRouter.shared().roles_index_text(compact=True)


def list_delegations_body() -> str:
    """Manager/parent view of spawn tree for this project/objective."""
    try:
        return get_job().bridge.list_delegations()
    except Exception as exc:
        return f"Error listing delegations: {exc}"


def propose_work_body(context_notes: str = "", max_agents: int = 4) -> str:
    """LLM-propose and spawn work (existing role ids only)."""
    try:
        n = int(str(max_agents).strip() or 4)
    except (TypeError, ValueError):
        n = 4
    try:
        return get_job().bridge.propose_agents(context_notes=context_notes or "", max_agents=n)
    except Exception as exc:
        return f"Error proposing work: {exc}"


