"""Project-mode planner: objectives JSON → markdown."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from orchestrator.crew.roles.registry import (
    RoleRegistry,
)
from orchestrator.planning.planner_base import PlannerBase
from orchestrator.utils.commands import normalize_objective_commands
from orchestrator.utils.paths import format_roe_block
from orchestrator.utils.strings import extract_json

PROJECT_PLAN_SYSTEM = """You are the PROJECT planner for an authorized Peon engagement (CrewAI roles).

Mission: decompose the engagement into kill-chain OBJECTIVES. You do not execute.
You produce the shared project plan (objectives + dependencies) for CrewAI roles.

## Hard constraints (RoE)
- Plan ONLY against in-scope subjects; never expand scope
- NEVER plan actions against exclusions
- If RoE / in-scope is empty: manager + passive/OSINT only; do not invent attack targets
- Technical / probing roles need in-scope values (engagement RoE)
- Project data lives under workspace/ and findings/

## Planning principles
- First objective MUST use the engagement manager role_id from the Role catalog
  (allow_delegation, empty reports_to)
- Last objective MUST use the analyzer role_id (capabilities include report)
- Keep 3–8 objectives total including those bookends
- Middle objectives MUST use a role_id from the Role catalog (never invent names)
  Match using goal + capabilities/tags + reports_to from the Role catalog block
- role_id is the PRIMARY role for the Job that runs the objective
- Every command binary MUST appear in that role's `binaries=[...]` catalog field.
  If an objective needs binaries owned by different roles, split it into separate
  role-owned objectives; never assign a forbidden binary to a role
- depends_on uses 1-based indices in THIS objectives list
- Independent middle objectives: each depends only on the manager bookend
  (index 1) so the scheduler can run them in parallel
- When a middle objective needs artifacts/targets/findings produced by an
  earlier middle, set depends_on to that earlier index (middle→middle is
  required for correct ordering — do not rely on agents to wait informally)
- Producer→consumer hand-offs (host/port scan → HTTP probing, recon → web
  testing, …): prefer a DISTINCT consumer objective with depends_on on its
  producer, so the consumer exists as a standby the producer can stream partial
  findings to mid-run — over folding the consumer into the producer objective or
  leaving it to runtime spawn_work
- Analyzer bookend waits until every middle specialist completes
- Technical experts run authorized scope as given. They may spawn_work for
  in-lane follow-on skills after evidence lands — never invent role ids, and
  never instruct CIDR/host-list subdivision into parallel workers unless the
  operator asked for that. Prefer plan-level depends_on over mid-job waits when
  ordering is known at plan time
- skill_ids on a middle objective: the **single lead skill** for that expert.
  Choose it by reading what each skill does in the Role catalog (its
  description), not by its name; the skill must be one the role lists. Do NOT
  dump the full role skill armory onto the objective — follow-ons are spawned
  at runtime
- The Role catalog also lists MCP servers bound to a role. When a skill or MCP
  server already implements the work (e.g. a pipeline for the whole task),
  plan the objective around it instead of rebuilding it from shell CLIs, and
  say so in the description. Restrictions that apply to shell probing do not
  silently apply to a skill/MCP step — state any limit explicitly
- No ethics lectures, no prose outside JSON

## Quality bar for each objective
- title: short, verb-led
- phase: recon | initial-access | post-exploit | reporting
- description: concrete actions on named in-scope subjects; do NOT tell the
  expert to partition CIDRs or fan out host-range workers
- acceptance_criteria: observable done condition
- mitre: technique ids when known; else []
- role_id: REQUIRED catalog role id
- skill_ids: one lead skill id for technical experts (from the role's skills)
- commands: 0–3 **plain shell CLIs** for the role (catalog binary first token),
  e.g. `nmap -sT --top-ports 100 -oG workspace/scan.gnmap <host>`.
  Leave empty when a skill / MCP server does the work.
  Do NOT wrap in `bash(...)` / `provision_cli(...)` / other tool-call syntax.
  Scope checks and provisioning are role workflow, not command strings.

## Output
Return ONLY a JSON object:
{
  "approach": "project",
  "goal": "one-line project goal tied to in-scope subjects",
  "objectives": [
    {
      "title": "short title",
      "phase": "recon | initial-access | post-exploit | reporting",
      "description": "what to do",
      "acceptance_criteria": "observable done condition",
      "mitre": ["T1046"],
      "depends_on": [1],
      "role_id": "<catalog role id — required>",
      "skill_ids": ["<lead skill id from that role's skills>"],
      "commands": ["nmap -sT --top-ports 100 -oG workspace/scan.gnmap <host>"]
    }
  ]
}
"""


def format_roles_block(role_ids: list[str] | None) -> str:
    """Optional agent-profile / role ids for profile_suggestion."""
    ids = [str(x).strip() for x in (role_ids or []) if str(x).strip()]
    if not ids:
        return ""
    return "Agent roles (optional profile_suggestion values):\n- " + "\n- ".join(ids)


def parse_project_objectives(raw: str) -> dict[str, Any]:
    """Parse project-plan JSON; tolerate fenced or prose-wrapped output."""
    text = extract_json(raw or "")
    try:
        data = json.loads(text)
    except Exception:
        return {
            "approach": "",
            "goal": "",
            "objectives": bookend_project_objectives([]),
        }
    if not isinstance(data, dict):
        return {
            "approach": "",
            "goal": "",
            "objectives": bookend_project_objectives([]),
        }
    objs = data.get("objectives") or []
    if not isinstance(objs, list):
        objs = []
    normalized: list[dict] = []
    for o in objs:
        if not isinstance(o, dict):
            continue
        row = dict(o)
        if not row.get("profile_suggestion") and row.get("profile"):
            row["profile_suggestion"] = row.get("profile")
        row["commands"] = normalize_objective_commands(row.get("commands"))
        normalized.append(row)
    data["objectives"] = bookend_project_objectives(normalized)
    return data


def _is_bookend_objective(obj: dict[str, Any]) -> bool:
    from orchestrator.crew.roles.catalog import resolve_role_id

    role = resolve_role_id(str(obj.get("role_id") or "").strip())   # an alias of a bookend role is that bookend
    phase = str(obj.get("phase") or "").strip().lower()
    start, end = RoleRegistry.shared().engagement_bookends()
    if role and role in {start, end}:
        return True
    return phase == "reporting"


def bookend_project_objectives(objectives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure every project plan starts with manager and ends with analyzer."""

    registry = RoleRegistry.shared()
    start, end = registry.engagement_bookends()
    if not start or not end:
        return [dict(o) for o in (objectives or []) if isinstance(o, dict)]

    raw = [dict(o) for o in (objectives or []) if isinstance(o, dict)]
    survivors: list[tuple[int, dict[str, Any]]] = []
    for idx, obj in enumerate(raw, start=1):
        if not _is_bookend_objective(obj):
            survivors.append((idx, obj))

    manager = {
        "title": "Project manager — review and dispatch",
        "phase": "recon",
        "description": (
            "Read RoE, review the persisted specialist objective sequence, confirm "
            "scope and dependencies. The scheduler releases every ready independent "
            "objective once this bookend completes (no serial stall)."
        ),
        "acceptance_criteria": "Specialist objective order and RoE constraints confirmed",
        "mitre": [],
        "depends_on": [],
        "role_id": start,
        "skill_ids": registry.objective_skill_ids(start),
        "commands": ["roe_status()"],
    }
    # old 1-based planner indices → new indices after manager bookend (1).
    old_to_new: dict[int, int] = {}
    kept: list[tuple[int, dict[str, Any]]] = []
    for old_idx, obj in survivors:
        role = str(obj.get("role_id") or "").strip()
        if not role:
            continue
        kept.append((old_idx, obj))
        old_to_new[old_idx] = 1 + len(kept)

    from orchestrator.crew.roles.catalog import resolve_role_id

    middle: list[dict[str, Any]] = []
    for old_idx, obj in kept:
        row = dict(obj)
        role = resolve_role_id(str(row.get("role_id") or "").strip())
        row["role_id"] = role
        raw_skills = row.get("skill_ids") or []
        if not isinstance(raw_skills, (list, tuple)):
            raw_skills = []
        row["skill_ids"] = registry.objective_skill_ids(role, raw_skills)
        remapped: list[int] = []
        for raw in row.get("depends_on") or []:
            try:
                dep = int(raw)
            except (TypeError, ValueError):
                continue
            if dep == old_idx:
                continue
            if dep in old_to_new:
                remapped.append(old_to_new[dep])
        # Parallel default: only the manager. Preserve remapped middle→middle
        # edges when the planner declared a real artifact/order dependency.
        row["depends_on"] = sorted(set(remapped)) or [1]
        middle.append(row)

    last_middle = 1 + len(middle)
    # Analyzer waits for every middle specialist (supports parallel middles).
    analyzer_deps = list(range(2, last_middle + 1)) if middle else [1]
    analyzer = {
        "title": "Findings report",
        "phase": "reporting",
        "description": (
            "Build findings/report.md covering the project's discoveries only "
            "(recorded findings and assets). Cite evidence; "
            "do not collect new evidence or invent discoveries. Do not report "
            "objective/job status. Write a findings report with these "
            "top-level sections, in order: Executive summary; Scope and "
            "methodology; Findings summary (ID/severity/asset table); "
            "Detailed findings (one block per finding: description, evidence, "
            "impact, remediation); Recommendations (prioritized); Appendix A "
            "Steps taken and commands executed; Appendix B Limitations."
        ),
        "acceptance_criteria": (
            "findings/report.md has Executive summary with overall risk "
            "rating, Scope and methodology, a Findings summary table, "
            "Detailed findings with evidence/impact/remediation per finding, "
            "prioritized Recommendations, and Appendix A listing every "
            "command executed"
        ),
        "mitre": [],
        "depends_on": analyzer_deps,
        "role_id": end,
        "skill_ids": registry.objective_skill_ids(end),
        "commands": [
            "list_findings()",
            "write_report_note(\"1. Executive summary\", \"…\")",
            "write_report_note(\"2. Scope and methodology\", \"…\")",
            "write_report_note(\"3. Findings summary\", \"| ID | Severity | Finding | Affected asset |\\n…\")",
            "write_report_note(\"4. Detailed findings\", \"### F-01 — …\\n…\")",
            "write_report_note(\"5. Recommendations\", \"…\")",
            "write_report_note(\"Appendix A. Steps taken and commands executed\", \"…\")",
            "write_report_note(\"Appendix B. Limitations\", \"…\")",
        ],
    }
    return [manager, *middle, analyzer]


def bookend_role_ids(names: list[str] | None) -> list[str]:
    """Put manager first and analyzer last; drop duplicates of either."""

    start, end = RoleRegistry.shared().engagement_bookends()
    bookends = {x for x in (start, end) if x}
    middle: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in bookends or name in seen:
            continue
        seen.add(name)
        middle.append(name)
    out: list[str] = []
    if start:
        out.append(start)
    out.extend(middle)
    if end:
        out.append(end)
    return out


def render_project_objectives(project_title: str, payload: dict[str, Any], objectives: list) -> str:
    """Render a project plan as markdown."""
    lines = [
        f"# Project plan — {project_title}",
        "",
        f"**Goal:** {(payload.get('goal') or '').strip() or '(see objectives)'}",
        f"**Approach:** {(payload.get('approach') or '').strip() or 'project'}",
        "",
        "## Objectives",
        "",
    ]
    for obj in objectives:
        mitre = ", ".join(obj.mitre_techniques or []) or "—"
        lines.append(
            f"{obj.seq}. **{obj.title}** [{obj.phase}] `{obj.status}` — {obj.description or ''}".rstrip()
        )
        lines.append(f"   - Acceptance: {obj.acceptance_criteria or '—'}")
        lines.append(f"   - MITRE: {mitre}")
        role = getattr(obj, "role_id", "") or ""
        if role:
            lines.append(f"   - Role: `{role}`")
        profile = getattr(obj, "profile_suggestion", "") or ""
        if profile:
            lines.append(f"   - Profile: `{profile}`")
        cmds = list(getattr(obj, "commands", None) or [])
        if cmds:
            lines.append("   - Commands (dry-run):")
            for cmd in cmds:
                lines.append(f"     - `{cmd}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _objectives_from_payload(payload: dict[str, Any]) -> list:
    objs = []
    for i, o in enumerate(payload.get("objectives") or [], start=1):
        objs.append(
            SimpleNamespace(
                seq=i,
                title=o.get("title") or f"Objective {i}",
                phase=o.get("phase") or "",
                status="pending",
                description=o.get("description") or "",
                acceptance_criteria=o.get("acceptance_criteria") or "",
                mitre_techniques=list(o.get("mitre") or []),
                role_id=o.get("role_id") or "",
                profile_suggestion=o.get("profile_suggestion") or o.get("profile") or "",
                commands=list(o.get("commands") or []),
            )
        )
    return objs


class ProjectPlanner(PlannerBase):
    plan_title = "Project plan"

    @property
    def system_prompt(self) -> str:
        return PROJECT_PLAN_SYSTEM

    def build_messages(
        self,
        description: str,
        *,
        project_title: str = "adhoc",
        project_summary: str = "",
        roe=None,
        prior_plan: str = "",
        transcript_digest: str = "",
        existing_objectives_summary: str = "",
        filtered_roles_index: str = "",
        focus_tags: list[str] | None = None,
        role_ids: list[str] | None = None,
    ) -> list:
        parts = [
            f"Project: {project_title}",
            f"Summary: {(project_summary or '').strip() or '(none)'}",
            f"Job brief:\n{(description or '').strip()}",
            format_roe_block(roe),
        ]
        if filtered_roles_index.strip():
            parts.append("Role catalog:\n" + filtered_roles_index.strip())
        if focus_tags:
            parts.append(
                "Project focus tags (preference, not exclusive): " + ", ".join(focus_tags)
            )
        roles = format_roles_block(role_ids)
        if roles:
            parts.append(roles)
        if existing_objectives_summary.strip():
            parts.append("Existing objectives:\n" + existing_objectives_summary.strip())
        self.append_context(
            parts,
            prior_plan=(prior_plan or "").strip()[:4000],
            transcript_digest=transcript_digest,
        )
        return self.wrap_messages(parts)

    def format_result(
        self,
        raw_llm_text: str,
        *,
        project_title: str = "adhoc",
        **_kwargs,
    ) -> str:
        raw = self.message_text(raw_llm_text) or ""
        payload = parse_project_objectives(raw)
        objs = _objectives_from_payload(payload)
        if objs:
            return render_project_objectives(project_title, payload, objs)
        return raw or "(empty plan)"
