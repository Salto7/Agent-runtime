"""LLM specialist proposals (CrewAI roles).

Used when an agent calls ``propose_agents``. Spawning stays in the control plane.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.utils.llm import chat_json, llm_configured


@dataclass(frozen=True)
class AgentSpec:
    """One proposed specialist Job under an objective."""

    title: str
    description: str
    role_id: str = ""


_PROPOSE_SYSTEM = """You plan parallel specialist agents for ONE engagement objective.
Return JSON only:
{"agents":[{"title":"...","description":"...","role_id":"..."}]}

Rules:
- Propose 0–N agents only when parallel specialists clearly help; else {"agents":[]}.
- Each agent gets a focused brief (description) derived from the objective context.
- role_id MUST be empty or one of the allowed role ids provided (prefer the
  objective's primary role when unsure — same-role fan-out is allowed).
- Do NOT invent role ids. Use transcript errors/gaps to choose better existing roles.
- Prefer fewer sharp specialists over many vague ones (max 4).
- Titles short; descriptions actionable under scope rules.
"""


def propose_agents(
    *,
    objective_title: str,
    objective_description: str,
    acceptance: str,
    primary_role: str,
    allowed_roles: list[str],
    context_notes: str = "",
    transcript_digest: str = "",
    max_agents: int = 4,
) -> list[AgentSpec]:
    """LLM-deduce specialist Jobs from objective context (empty if not useful)."""
    if not llm_configured():
        return []
    allow = [s.strip() for s in allowed_roles if str(s).strip()]
    primary = (primary_role or "").strip()
    if primary and primary not in allow:
        allow = [primary, *allow]
    digest = (transcript_digest or "").strip()
    human = (
        f"Primary role: {primary or '(none)'}\n"
        f"Allowed role ids: {', '.join(allow) or '(none)'}\n"
        f"Max agents: {max(0, min(8, int(max_agents)))}\n\n"
        f"Objective title: {objective_title}\n"
        f"Description: {objective_description or '(none)'}\n"
        f"Acceptance: {acceptance or '(none)'}\n\n"
        f"Extra context:\n{(context_notes or '(none)')[:4000]}\n"
    )
    if digest:
        human += f"\nTranscript digest (errors/handoffs/gaps):\n{digest[:4000]}\n"
    try:
        from orchestrator.utils import llm_usage

        with llm_usage.purpose("agent_propose"):
            data = chat_json(_PROPOSE_SYSTEM, human)
    except Exception:
        return []
    rows = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[AgentSpec] = []
    allow_set = set(allow)
    for row in rows[: max(0, min(8, int(max_agents)))]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()[:255]
        desc = str(row.get("description") or "").strip()[:4000]
        role = str(row.get("role_id") or primary or "").strip()
        if role and allow_set and role not in allow_set:
            role = primary
        if not title or not desc:
            continue
        out.append(AgentSpec(title=title, description=desc, role_id=role))
    return out
