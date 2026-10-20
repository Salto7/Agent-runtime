"""Per-agent transcript → curated gist.

Each finished agent run is a :class:`~orchestrator.crew.runs.RunRecord` (supplied
by the host's ``RunSource``). This module compacts it, supplies it to Cognee as that agent's session document, and
asks the LLM to curate a *gist*: what the task was, what worked, what failed,
and concrete advice for planning a similar objective. Gists are stored as
memory entries (platform scope when they are target-agnostic, otherwise
project scope), so planners and agents recall lessons instead of raw logs.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from orchestrator.catalog import PlatformCatalog
from orchestrator.crew.runs import RunRecord
from orchestrator.memory import store as memory_store
from orchestrator.memory.models import CatalogProposal

logger = logging.getLogger(__name__)

# Characters of transcript handed to the curator. This bounds the *LLM input*;
# the middle of an over-long run is replaced by an explicit omission marker.
TRANSCRIPT_BUDGET = 28_000
_LINE_CAP = 260
_MIN_TOOL_CALLS = 3

# Lifecycle chatter that says nothing about how the work went.
_NOISE = (
    "Running roles:",
    "sandbox docker:",
    "verified CLIs:",
    "Agent runtime",
    "crewai role=",
    "provisioned:",
    "Woken:",
)

SYSTEM_PROMPT = (
    "You curate one agent's work log into a GIST that helps people plan "
    "similar work later. Be concrete and honest about failures, including "
    "tooling or environment problems. Generalise: NEVER include target names, "
    "company names, domains, hostnames, IPs, URLs, emails or other identifiers "
    "from the log — describe them by kind ('a corporate domain', 'a /24'). "
    "Return JSON only, with keys: "
    '"outcome" ("success"|"partial"|"failed"), '
    '"task_kind" (short generic label), '
    '"approach" (2-4 sentences), '
    '"worked" (list of short strings), '
    '"failed" (list of short strings), '
    '"plan_advice" (list of concrete recommendations for planning a similar '
    "objective: skills/tools to use, how to split the work, checks to include), "
    '"avoid" (list of pitfalls). Use empty lists when there is nothing to say.'
)


def _one_line(text: str, cap: int = _LINE_CAP) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= cap else flat[: cap - 1] + "…"


def render_transcript(job: RunRecord, rows: list[Any] | None = None, *, budget: int = TRANSCRIPT_BUDGET) -> str:
    """Compact, readable transcript: commands, key output, errors, outcome."""
    rows = job.events if rows is None else rows
    lines = [
        f"AGENT: {job.title}",
        f"ROLE: {', '.join(job.role_ids or []) or '-'}  SKILLS: {', '.join(job.skill_ids or []) or '-'}",
        f"STATUS: {job.status}" + (f"  ERROR: {_one_line(job.error)}" if job.error else ""),
    ]
    if job.objective_title:
        lines.append(
            f"OBJECTIVE: {_one_line(job.objective_title)} — accept: {_one_line(job.acceptance or '')}"
        )
    if job.description:
        lines.append(f"BRIEF: {_one_line(job.description, 600)}")
    lines.append("--- LOG ---")
    body: list[str] = []
    for row in rows:
        kind = str(row.get("message_type") or "")
        text = str(row.get("content") or "")
        if kind == "tool":
            body.append(f"$ {_one_line(text)}")
        elif kind == "stdout":
            body.append(f"  out: {_one_line(text)}")
        elif kind == "stderr":
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            tag = "err" if meta.get("exit_code") != 0 else "note"
            body.append(f"  {tag}: {_one_line(text)}")
        elif kind == "error":
            body.append(f"ERROR: {_one_line(text, 400)}")
        elif kind == "result":
            body.append(f"RESULT: {_one_line(text, 1500)}")
        elif text and not text.startswith(_NOISE):
            body.append(f"- {_one_line(text, 200)}")
    deduped: list[str] = []
    for line in body:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    text = "\n".join(deduped)
    if len(text) > budget:
        head = int(budget * 0.35)
        tail = budget - head
        omitted = len(text) - budget
        text = f"{text[:head]}\n[… {omitted} chars of the middle omitted …]\n{text[-tail:]}"
    return "\n".join(lines) + "\n" + text


_ORCHESTRATION = ("Delegation:", "Operator input needed")
_AGENT_MESSAGE = re.compile(r"\[(request|handoff|inform)\]")


def _substantive(row: dict[str, Any]) -> bool:
    """A tool call, or an orchestration step (spawn, handoff, operator ask)."""
    if row.get("message_type") == "tool":
        return True
    text = str(row.get("content") or "")
    return text.startswith(_ORCHESTRATION) or bool(_AGENT_MESSAGE.search(text[:120]))


def worth_curating(job: RunRecord, rows: list[Any] | None = None) -> bool:
    """Skip trivial runs: a lesson needs actual work or a failure to learn from.

    A supervisor that only spawns workers and exchanges handoffs has few tool
    calls of its own but is exactly where planning lessons come from.
    """
    rows = job.events if rows is None else rows
    work = sum(1 for r in rows if _substantive(r))
    return work >= _MIN_TOOL_CALLS or job.status == "failed" or bool(job.error)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "task"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [" ".join(str(v).split()) for v in value if str(v).strip()]
    return []


def render_gist(data: dict[str, Any]) -> tuple[str, str]:
    """``(description, body)`` for a memory entry from the curator's JSON."""
    outcome = str(data.get("outcome") or "partial").lower()
    kind = " ".join(str(data.get("task_kind") or "task").split())
    advice = _as_list(data.get("plan_advice"))
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
        items = _as_list(data.get(key))
        if items:
            parts.append(f"**{label}:**")
            parts += [f"- {i}" for i in items]
            parts.append("")
    return desc, "\n".join(parts).strip()


def curate_gist(job: RunRecord, transcript: str) -> dict[str, Any] | None:
    """LLM-curated gist for one agent, or None when the LLM is unavailable."""
    from orchestrator.utils.llm import chat_json, llm_configured

    if not llm_configured():
        return None
    try:
        from orchestrator.utils import llm_usage

        with llm_usage.purpose("dream_gist_curation"):
            data = chat_json(SYSTEM_PROMPT, transcript)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gist curation failed for job %s: %s", job.job_id, exc)
        return None
    return data if isinstance(data, dict) else None


def store_gist(job: RunRecord, data: dict[str, Any]) -> str:
    """Persist as a platform lesson; fall back to project scope if identifiers leak."""
    desc, body = render_gist(data)
    name = f"gist-{_slug(str(data.get('task_kind') or ''))}-{str(job.job_id).replace('-', '')[:8]}"
    role = (job.role_ids or [""])[0]
    try:
        memory_store.save(
            None, name=name, kind="lesson", description=desc, body=body,
            role_id=role, created_by="dream", scope_values=job.scope_values,
        )
        return "platform"
    except ValueError as exc:
        logger.info("gist %s kept project-scoped: %s", name, exc)
    if not job.project_id:
        raise ValueError("gist is target-specific and the run has no project to keep it in")
    memory_store.save(
        job.project_id, name=name, kind="lesson", description=desc, body=body,
        role_id=role, job_id=job.job_id, created_by="dream",
    )
    return "project"


def supply_transcript(job: RunRecord, transcript: str) -> bool:
    """Give Cognee this agent's transcript as its own session document."""
    from orchestrator.crew.cognee_backend import CogneeBackend
    from orchestrator.crew.memory import job_session_id, memory_enabled, project_dataset

    if not memory_enabled():
        return False
    doc = {
        "kind": "transcript",
        "job_id": str(job.job_id),
        "title": job.title,
        "role_id": (job.role_ids or [""])[0],
        "status": job.status,
        "project_id": str(job.project_id),
        "summary": transcript,
        "indications": ["transcript", "agent"],
    }
    result = CogneeBackend.shared().remember(
        doc,
        dataset_name=project_dataset(str(job.project_id)),
        session_id=job_session_id(job),
        self_improvement=False,
        run_in_background=True,
    )
    return bool(result.get("ok"))


def process_job(job: RunRecord) -> dict[str, Any]:
    """Supply the transcript and curate the gist for one finished agent."""
    rows = job.events
    out: dict[str, Any] = {"job_id": str(job.job_id), "title": job.title}
    if not worth_curating(job, rows):
        return {**out, "skipped": "trivial"}
    transcript = render_transcript(job, rows)
    out["supplied"] = supply_transcript(job, transcript)
    data = curate_gist(job, transcript)
    if data is None:
        return {**out, "gist": False, "retry": True}
    out["scope"] = store_gist(job, data)
    out["gist"] = True
    return out


# ── cross-run synthesis ──────────────────────────────────────────────────

SYNTH_MIN_NEW = 5
SYNTH_INPUT_BUDGET = 40_000
_SYNTH_PER_GIST = 1_400

SYNTH_SYSTEM = (
    "You consolidate many per-agent gists into overall lessons learned. Merge "
    "duplicates, favour patterns that recur across runs, and drop one-offs. "
    "Keep everything generic: NEVER include target, company or domain names, "
    "IPs, URLs or emails. Separate two things: (1) how to PLAN and RUN a kind of "
    "task well, and (2) defects or gaps in the platform/tooling that agents ran "
    "into (missing output, wrong defaults, silent limits, unavailable "
    "dependencies). Return JSON only: "
    '{"playbooks":[{"task_kind":"short generic label","when_to_use":"one line",'
    '"recommended_plan":["ordered, concrete steps/splits/checks"],'
    '"pitfalls":["..."],"tooling_notes":["..."],"evidence":<number of gists>}],'
    '"platform_improvements":[{"issue":"...","seen_in":<number>,'
    '"suggested_fix":"..."}]}. At most 8 playbooks.'
)


def _platform_gists() -> list[Any]:
    entries = [e for e in memory_store._scope(None) if e.name.startswith("gist-")]
    entries.sort(key=memory_store.entry_updated, reverse=True)
    return entries


def synthesize_playbooks() -> dict[str, Any]:
    """Consolidate platform gists into playbooks + a platform-improvements note."""
    from orchestrator.utils.llm import chat_json, llm_configured

    gists = _platform_gists()
    if not gists or not llm_configured():
        return {"ok": False, "reason": "no gists" if not gists else "llm not configured"}
    chunks: list[str] = []
    used = 0
    for e in gists:
        block = f"### {e.name}\n{(e.body or e.description).strip()[:_SYNTH_PER_GIST]}"
        if used + len(block) > SYNTH_INPUT_BUDGET:
            break
        chunks.append(block)
        used += len(block)
    try:
        from orchestrator.utils import llm_usage

        with llm_usage.purpose("dream_synthesis"):
            data = chat_json(SYNTH_SYSTEM, "\n\n".join(chunks))
    except Exception as exc:  # noqa: BLE001
        logger.warning("playbook synthesis failed: %s", exc)
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
            items = _as_list(pb.get(key))
            if items:
                body_parts.append(f"**{label}:**")
                body_parts += [f"- {i}" for i in items]
                body_parts.append("")
        body_parts.append(f"_Consolidated from {int(pb.get('evidence') or 0)} run gist(s)._")
        try:
            memory_store.save(
                None,
                name=f"playbook-{_slug(kind)}",
                kind="lesson",
                description=f"{kind}: {' '.join(str(pb.get('when_to_use') or '').split())}"[:240],
                body="\n".join(body_parts).strip(),
                created_by="dream",
            )
            saved.append(f"playbook-{_slug(kind)}")
        except ValueError as exc:
            logger.info("playbook %r skipped: %s", kind, exc)
    improvements = _as_improvements(data.get("platform_improvements") if isinstance(data, dict) else None)
    if improvements:
        try:
            memory_store.save(
                None,
                name="platform-improvements",
                kind="reference",
                description="Recurring platform/tool issues seen in agent runs, with suggested fixes",
                body=improvements,
                created_by="dream",
            )
            saved.append("platform-improvements")
        except ValueError as exc:
            logger.info("platform-improvements skipped: %s", exc)
    return {"ok": True, "gists": len(gists), "saved": saved}


def _as_improvements(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    lines: list[str] = []
    for item in value:
        if not isinstance(item, dict) or not str(item.get("issue") or "").strip():
            continue
        issue = " ".join(str(item["issue"]).split())
        fix = " ".join(str(item.get("suggested_fix") or "").split())
        seen = int(item.get("seen_in") or 0)
        lines.append(f"- **{issue}** (seen in {seen} run(s))" + (f"\n  Fix: {fix}" if fix else ""))
    return "\n".join(lines)


def maybe_synthesize(cursor: dict[str, Any], *, now: str) -> dict[str, Any] | None:
    """Synthesize when enough new gists have accumulated since the last pass."""
    total = len(_platform_gists())
    last = cursor.get("synthesis") if isinstance(cursor.get("synthesis"), dict) else {}
    if total - int(last.get("gist_count") or 0) < SYNTH_MIN_NEW:
        return None
    result = synthesize_playbooks()
    if result.get("ok"):
        cursor["synthesis"] = {"at": now, "gist_count": total, "saved": result.get("saved", [])}
    return result


# ── catalog proposals (Dream suggests role/skill changes for review) ───────

PROPOSE_SYSTEM = (
    "You are a catalog curator. You are given (1) playbooks distilled from many "
    "agent runs, (2) a note on recurring platform/tooling issues, and (3) the "
    "current catalog of engagement roles and skills. Suggest concrete changes "
    "to the roles/skills catalog that would make future runs of the same kind "
    "of work better: a new role when a recurring task_kind has no good fit "
    "among the existing roles, an edit to an existing role's goal/backstory/"
    "aliases when a playbook implies it should approach its job differently, "
    "a new skill (as a full SKILL.md) when plan_advice/tooling_notes describe "
    "reusable step-by-step instructions no existing skill covers, or an edit to "
    "an existing skill's SKILL.md to fix a described gap. "
    "NEVER propose changing a role's `kind`, and never propose editing a role "
    "marked protected except to adjust its goal/backstory text. Never invent "
    "target/company/domain names. Propose at most 4 changes; propose none if "
    "nothing in the input clearly calls for one. For action=update, the role's "
    "`role_id` (or the skill_md frontmatter `name:`) must equal target_id — "
    "you are editing that existing one, not creating a new one. "
    "A SKILL.md starts with YAML frontmatter (--- / name / description / --- ) "
    "followed by markdown instructions. "
    "Return JSON only: {\"proposals\":[{"
    '"kind":"role"|"skill","action":"create"|"update",'
    '"target_id":"existing role_id/skill_id, or \\"\\" for create",'
    '"title":"short label for a reviewer","rationale":"1-3 sentences: which '
    'playbook/issue this addresses and why",'
    '"role":{"role_id":"...","kind":"engagement-pm|analyzer|technical",'
    '"aliases":["..."],"goal":"...","backstory":"..."} (role proposals only),'
    '"skill_md":"full SKILL.md text" (skill proposals only)'
    "}]}"
)
PROPOSE_INPUT_BUDGET = 20_000
PROPOSE_MAX_PENDING = 12


def _catalog_snapshot() -> str:
    from orchestrator.catalog import PlatformCatalog

    catalog = PlatformCatalog.shared()
    lines = ["ROLES:"]
    for role in catalog.list_roles():
        goal = " ".join((role.goal or "").split())[:160]
        lines.append(
            f"- {role.id} (kind={role.kind}, protected={role.is_protected}): {goal}"
        )
    lines.append("")
    lines.append("SKILLS:")
    for skill in catalog.list_skills():
        desc = " ".join((skill.description or "").split())[:160]
        lines.append(f"- {skill.skill_id}: {desc}")
    return "\n".join(lines)


def _recent_lessons() -> str:
    entries = [
        e
        for e in memory_store._scope(None)
        if e.name.startswith("playbook-") or e.name == "platform-improvements"
    ]
    entries.sort(key=memory_store.entry_updated, reverse=True)
    chunks: list[str] = []
    used = 0
    for e in entries:
        block = f"### {e.name}\n{(e.body or e.description).strip()}"
        if used + len(block) > PROPOSE_INPUT_BUDGET:
            break
        chunks.append(block)
        used += len(block)
    return "\n\n".join(chunks)


def _existing_pending(kind: str, action: str, target_id: str) -> bool:
    return CatalogProposal.objects.filter(
        status="pending", kind=kind, action=action, target_id=target_id
    ).exists()


def propose_catalog_changes() -> dict[str, Any]:
    """Ask the LLM for role/skill catalog changes and stage them for review.

    Nothing here ever touches the live catalog — each suggestion becomes a
    pending ``CatalogProposal`` row; a human approves or rejects it from the
    Catalog page.
    """
    from orchestrator.utils.llm import chat_json, llm_configured

    if not llm_configured():
        return {"ok": False, "reason": "llm not configured"}
    pending_count = CatalogProposal.objects.filter(status="pending").count()
    if pending_count >= PROPOSE_MAX_PENDING:
        return {"ok": False, "reason": "pending queue full", "pending": pending_count}
    lessons = _recent_lessons()
    if not lessons.strip():
        return {"ok": False, "reason": "no playbooks or platform-improvements yet"}
    user_input = f"{lessons}\n\n---\n\nCURRENT CATALOG\n{_catalog_snapshot()}"
    try:
        from orchestrator.utils import llm_usage

        with llm_usage.purpose("dream_catalog_proposals"):
            data = chat_json(PROPOSE_SYSTEM, user_input)
    except Exception as exc:  # noqa: BLE001
        logger.warning("catalog proposal generation failed: %s", exc)
        return {"ok": False, "reason": str(exc)[:200]}
    raw = data.get("proposals") if isinstance(data, dict) else None
    created: list[str] = []
    for item in (raw or [])[:4]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        action = str(item.get("action") or "").strip().lower()
        target_id = str(item.get("target_id") or "").strip().lower()
        if kind not in {"role", "skill"} or action not in {"create", "update"}:
            continue
        if action == "update" and not target_id:
            continue
        if action == "create" and target_id:
            continue
        if target_id and _existing_pending(kind, action, target_id):
            continue
        if kind == "role":
            role = item.get("role") if isinstance(item.get("role"), dict) else {}
            if not str(role.get("goal") or "").strip():
                continue
            payload: dict[str, Any] = {"role": role}
        else:
            skill_md = str(item.get("skill_md") or "").strip()
            if not skill_md:
                continue
            payload = {"skill_md": skill_md}
        row = CatalogProposal(
            id=uuid.uuid4().hex,
            kind=kind,
            action=action,
            target_id=target_id,
            title=" ".join(str(item.get("title") or "").split())[:200]
            or f"{action} {kind} {target_id or '(new)'}",
            rationale=" ".join(str(item.get("rationale") or "").split()),
            payload=json.dumps(payload),
            created_at=_now_dt(),
        )
        row.save(force_insert=True)
        created.append(row.id)
    return {"ok": True, "created": created}


def list_proposals(status: str = "pending") -> list[CatalogProposal]:
    """Staged proposals (newest first)."""
    rows = list(CatalogProposal.objects.filter(status=status))
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return rows


def apply_proposal(proposal_id: str, *, decision: str) -> dict[str, Any]:
    """Approve (writes the live catalog) or reject a pending proposal."""
    if decision not in {"approve", "reject"}:
        return {"ok": False, "error": "decision must be approve or reject"}
    row = CatalogProposal.objects.filter(id=str(proposal_id), status="pending").first()
    if row is None:
        return {"ok": False, "error": "proposal not found or already decided"}
    if decision == "reject":
        row.status = "rejected"
        row.decided_at = _now_dt()
        row.save()
        return {"ok": True, "status": "rejected"}
    catalog = PlatformCatalog.shared()
    payload = json.loads(row.payload or "{}")
    try:
        if row.kind == "role":
            role = payload.get("role") or {}
            existing = catalog.get_role(row.target_id) if row.target_id else None
            catalog.upsert_role(
                role_id=str(role.get("role_id") or row.target_id or ""),
                kind=str(role.get("kind") or (existing.kind if existing else "")),
                aliases=role.get("aliases") or (list(existing.aliases) if existing else []),
                previous_id=row.target_id,
                goal=str(role.get("goal") or ""),
                backstory=str(role.get("backstory") or ""),
            )
        elif row.kind == "skill":
            current = catalog.get_skill(row.target_id) if row.target_id else None
            catalog.upsert_skill(
                skill_md=str(payload.get("skill_md") or ""),
                skill_id=row.target_id,
                files=dict(current.files) if current else None,   # an update keeps the pack's files and bindings
                keep_bindings=current is not None,
            )
        else:
            raise ValueError(f"unknown proposal kind {row.kind!r}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    row.status = "approved"
    row.decided_at = _now_dt()
    row.save()
    return {"ok": True, "status": "approved"}


def _now_dt() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
