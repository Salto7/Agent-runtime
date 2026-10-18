"""Dream: curate finished-run transcripts into lessons, consolidate repeated
lessons into playbooks, and propose role changes from what was learned —
every proposal staged for human review, nothing applied automatically.

Domain-neutral by design: the prompts below say nothing about any specific
kind of agent work. A host with a particular domain (security engagements,
support triage, whatever) should pass its own system-prompt text in rather
than this module growing domain flavor.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from orchestrator.brain.protocols import (
    LessonRecord,
    MemoryStore,
    ProposalRecord,
    ProposalStore,
    RoleRecord,
    RoleStore,
)

ChatJSON = Callable[[str, str], dict[str, Any]]

GIST_SYSTEM = (
    "You curate one agent's work log into a GIST that helps people plan "
    "similar work later. Be concrete and honest about failures, including "
    "tooling or environment problems. Generalize: NEVER include identifying "
    "names (people, organizations, hosts, URLs, credentials) from the log — "
    "describe them by kind. Return JSON only, with keys: "
    '"outcome" ("success"|"partial"|"failed"), "task_kind" (short generic label), '
    '"approach" (2-4 sentences), "worked" (list of short strings), '
    '"failed" (list of short strings), "plan_advice" (list of concrete '
    "recommendations for planning similar work: how to split it, checks to "
    'include), "avoid" (list of pitfalls). Use empty lists when there is '
    "nothing to say."
)

SYNTH_SYSTEM = (
    "You consolidate many per-run gists into overall lessons learned. Merge "
    "duplicates, favour patterns that recur across runs, and drop one-offs. "
    "Keep everything generic: never include identifying names. Separate two "
    "things: (1) how to plan and run a kind of task well, and (2) defects or "
    "gaps in the platform/tooling runs kept hitting. Return JSON only: "
    '{"playbooks":[{"task_kind":"short generic label","when_to_use":"one line",'
    '"recommended_plan":["ordered, concrete steps/splits/checks"],'
    '"pitfalls":["..."],"tooling_notes":["..."],"evidence":<number of gists>}],'
    '"platform_improvements":[{"issue":"...","seen_in":<number>,'
    '"suggested_fix":"..."}]}. At most 8 playbooks.'
)

PROPOSE_SYSTEM = (
    "You are a catalog curator. Given playbooks distilled from many runs, a "
    "note on recurring platform issues, and the current roster of roles, "
    "suggest concrete role changes that would make future runs of the same "
    "kind of work better: a new role when a recurring task_kind has no good "
    "fit among existing roles, or an edit to an existing role's goal/"
    "backstory when a playbook implies it should approach its job "
    "differently. Never propose changing a protected role's identity. "
    "Propose at most 4 changes; propose none if nothing clearly calls for "
    "one. For action=update, role_id must be an existing role from the "
    "roster above — you are editing that one, not creating a new one. "
    'Return JSON only: {"proposals":[{"action":"create"|"update",'
    '"title":"short label for a reviewer","rationale":"1-3 sentences: '
    'which playbook/issue this addresses and why","role":{"role_id":"...",'
    '"kind":"worker","aliases":["..."],"goal":"...","backstory":"..."}}]}'
)

SYNTH_MIN_NEW = 5
PROPOSE_MAX_PENDING = 12


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "task"


def curate_gist(transcript: str, *, chat_json: ChatJSON) -> dict[str, Any] | None:
    """One finished run's transcript -> a generalized lesson, or ``None``
    when the model call fails (caller should retry later, not treat it as
    "nothing to learn")."""
    try:
        data = chat_json(GIST_SYSTEM, transcript)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def render_gist(job_id: str, role_id: str, data: dict[str, Any]) -> LessonRecord:
    outcome = str(data.get("outcome") or "partial").lower()
    kind = " ".join(str(data.get("task_kind") or "task").split())
    advice = [str(x) for x in (data.get("plan_advice") or []) if str(x).strip()]
    desc = f"{kind}: {outcome}" + (f" — {advice[0]}" if advice else "")
    parts = [f"**Task:** {kind} (outcome: {outcome})", ""]
    if data.get("approach"):
        parts += [f"**Approach:** {' '.join(str(data['approach']).split())}", ""]
    for label, key in (
        ("Worked", "worked"),
        ("Failed / went wrong", "failed"),
        ("Plan advice", "plan_advice"),
        ("Avoid", "avoid"),
    ):
        items = [str(x) for x in (data.get(key) or []) if str(x).strip()]
        if items:
            parts.append(f"**{label}:**")
            parts += [f"- {i}" for i in items]
            parts.append("")
    body = "\n".join(parts).strip()
    name = f"gist-{_slug(kind)}-{job_id[:8]}"
    return LessonRecord(name=name, task_kind=kind, description=desc, body=body, role_id=role_id)


def synthesize_playbooks(
    memory: MemoryStore, *, chat_json: ChatJSON, min_new: int = SYNTH_MIN_NEW
) -> dict[str, Any]:
    """Consolidate accumulated gists into playbooks + a platform-improvements note."""
    gists = memory.list_lessons(prefix="gist-")
    if len(gists) < min_new:
        return {"ok": False, "reason": "not enough gists yet", "count": len(gists)}
    chunks = [f"### {g.name}\n{g.body}" for g in gists[:60]]
    try:
        data = chat_json(SYNTH_SYSTEM, "\n\n".join(chunks))
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200]}
    saved: list[str] = []
    for pb in (data.get("playbooks") or [])[:8] if isinstance(data, dict) else []:
        if not isinstance(pb, dict):
            continue
        kind = " ".join(str(pb.get("task_kind") or "").split())
        if not kind:
            continue
        body_parts = [f"**When to use:** {' '.join(str(pb.get('when_to_use') or '').split())}", ""]
        for label, key in (
            ("Recommended plan", "recommended_plan"),
            ("Pitfalls", "pitfalls"),
            ("Tooling notes", "tooling_notes"),
        ):
            items = [str(x) for x in (pb.get(key) or []) if str(x).strip()]
            if items:
                body_parts.append(f"**{label}:**")
                body_parts += [f"- {i}" for i in items]
                body_parts.append("")
        body_parts.append(f"_Consolidated from {int(pb.get('evidence') or 0)} run(s)._")
        name = f"playbook-{_slug(kind)}"
        memory.save_playbook(
            name,
            f"{kind}: {' '.join(str(pb.get('when_to_use') or '').split())}"[:240],
            "\n".join(body_parts).strip(),
        )
        saved.append(name)
    improvements = (data.get("platform_improvements") or []) if isinstance(data, dict) else []
    lines = []
    for item in improvements:
        if not isinstance(item, dict) or not str(item.get("issue") or "").strip():
            continue
        issue = " ".join(str(item["issue"]).split())
        fix = " ".join(str(item.get("suggested_fix") or "").split())
        seen = int(item.get("seen_in") or 0)
        lines.append(f"- **{issue}** (seen in {seen} run(s))" + (f"\n  Fix: {fix}" if fix else ""))
    if lines:
        memory.save_playbook(
            "platform-improvements",
            "Recurring platform/tooling issues seen in agent runs, with suggested fixes",
            "\n".join(lines),
        )
        saved.append("platform-improvements")
    return {"ok": True, "gists": len(gists), "saved": saved}


def propose_role_changes(
    roles: RoleStore,
    memory: MemoryStore,
    proposals: ProposalStore,
    *,
    chat_json: ChatJSON,
    max_pending: int = PROPOSE_MAX_PENDING,
) -> dict[str, Any]:
    """Ask the model for role changes from accumulated playbooks; stage them.

    Never touches ``roles`` directly — every suggestion becomes a pending
    ``ProposalRecord``; ``apply_proposal`` is the only path from here to a
    live role, and it requires an explicit decision.
    """
    if len(proposals.list_proposals(status="pending")) >= max_pending:
        return {"ok": False, "reason": "pending queue full"}
    lessons = [
        lesson
        for lesson in memory.list_lessons()
        if lesson.name.startswith("playbook-") or lesson.name == "platform-improvements"
    ]
    if not lessons:
        return {"ok": False, "reason": "no playbooks or platform-improvements yet"}
    lessons_text = "\n\n".join(f"### {lesson.name}\n{lesson.body}" for lesson in lessons)
    roster = "\n".join(
        f"- {r.role_id} (kind={r.kind}, protected={r.protected}): {r.goal[:160]}"
        for r in roles.list_roles()
    )
    try:
        data = chat_json(PROPOSE_SYSTEM, f"{lessons_text}\n\n---\n\nCURRENT ROLES\n{roster}")
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200]}
    created: list[str] = []
    for item in (data.get("proposals") or [])[:4] if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower()
        role = item.get("role") if isinstance(item.get("role"), dict) else {}
        role_id = str(role.get("role_id") or "").strip().lower()
        if (
            action not in {"create", "update"}
            or not role_id
            or not str(role.get("goal") or "").strip()
        ):
            continue
        if action == "update" and roles.get_role(role_id) is None:
            continue  # nothing to update
        # Dedupe on the role this proposal is actually about — a "create"
        # has no pre-existing target_id to key off, but it does have the
        # role_id the model chose, which is what a repeat suggestion reuses.
        if proposals.find_pending(kind="role", target_id=role_id):
            continue
        row = proposals.create_proposal(
            ProposalRecord(
                id="",
                kind="role",
                action=action,
                target_id=role_id,
                title=" ".join(str(item.get("title") or "").split())[:200]
                or f"{action} role {role_id}",
                rationale=" ".join(str(item.get("rationale") or "").split()),
                payload={"role": role},
            )
        )
        created.append(row.id)
    return {"ok": True, "created": created}


def apply_proposal(
    roles: RoleStore, proposals: ProposalStore, proposal_id: str, *, decision: str
) -> dict[str, Any]:
    """Approve (writes the live role) or reject a pending proposal."""
    if decision not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    row = proposals.get_proposal(proposal_id)
    if row is None or row.status != "pending":
        return {"ok": False, "error": "proposal not found or already decided"}
    if decision == "reject":
        proposals.decide_proposal(proposal_id, "rejected")
        return {"ok": True, "status": "rejected"}
    role = row.payload.get("role") or {}
    role_id = str(role.get("role_id") or row.target_id or "").strip().lower()
    if not role_id:
        return {"ok": False, "error": "proposal payload is missing role_id"}
    existing = roles.get_role(role_id)
    if existing is not None and existing.protected:
        return {"ok": False, "error": f"role {role_id!r} is protected and cannot be replaced"}
    roles.upsert_role(
        RoleRecord(
            role_id=role_id,
            kind=str(role.get("kind") or (existing.kind if existing else "worker")),
            aliases=tuple(role.get("aliases") or ()),
            goal=str(role.get("goal") or ""),
            backstory=str(role.get("backstory") or ""),
            skills=existing.skills if existing else (),
            protected=existing.protected if existing else False,
        )
    )
    proposals.decide_proposal(proposal_id, "approved")
    return {"ok": True, "status": "approved", "role_id": role_id}
