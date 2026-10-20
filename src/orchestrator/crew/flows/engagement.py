"""Build a hierarchical engagement Crew from platform role hierarchy."""

from __future__ import annotations

from typing import Any

from orchestrator.agent.config import AgentRunConfig
from orchestrator.crew.roles.model import build_crew_agent, llm_id_for_crew
from orchestrator.crew.roles.registry import RoleRegistry


def build_engagement_crew(
    *,
    brief: str,
    role_ids: tuple[str, ...] | list[str] | None = None,
    replan_note: str = "",
    config: AgentRunConfig | None = None,
) -> Any:
    """Return a CrewAI ``Crew`` (hierarchical + planning). Lazy-imports crewai."""
    try:
        # deferred: optional heavy crewai
        from crewai import Crew, Process, Task
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "crewai is required for AGENT_MODULE=crewai "
            "(pip install 'crewai>=1.0.0')"
        ) from exc

    cfg = config or AgentRunConfig()
    agent_kwargs = {
        "max_iterations": cfg.max_iterations,
        "reasoning_effort": cfg.crew_reasoning_effort,
        "reasoning_max_attempts": cfg.crew_reasoning_max_attempts,
    }

    reg = RoleRegistry.shared()
    manager_spec = reg.manager()
    analyzer_spec = reg.analyzer()
    if manager_spec is None:
        raise RuntimeError(
            "No manager role in platform store (need kind: engagement-pm)"
        )
    if analyzer_spec is None:
        raise RuntimeError(
            "No analyzer role in platform store (need kind: analyzer)"
        )


    specialist_specs = reg.specialists(role_ids)
    # CrewAI hierarchical mode injects delegation tools into a custom manager
    # and rejects manager_agent instances that already carry role tools.
    manager = build_crew_agent(manager_spec, tools=[], collaborate=True, **agent_kwargs)
    # Specialists collaborate peer-to-peer via CrewAI's native coworker tools
    # (ask/delegate), so they can consult each other, not only the manager.
    specialists = [
        build_crew_agent(s, collaborate=True, **agent_kwargs) for s in specialist_specs
    ]
    analyzer = build_crew_agent(analyzer_spec, **agent_kwargs)

    specialist_lines = ", ".join(s.id for s in specialist_specs) or "(none preselected)"
    plan_extra = ""
    if (replan_note or "").strip():
        plan_extra = (
            "\n\nOPERATOR REPLAN / STEER:\n"
            f"{replan_note.strip()[:4000]}\n"
            "Revise the remaining plan accordingly under RoE.\n"
        )

    engagement_task = Task(
        description=(
            "Authorized Peon engagement.\n\n"
            f"BRIEF:\n{(brief or '').strip()[:6000]}\n"
            f"{plan_extra}\n"
            f"Available specialists (reports_to={manager_spec.id}): {specialist_lines}\n"
            "As engagement manager:\n"
            "1. Read authorized targets and exclusions from the BRIEF; do not expand scope.\n"
            "2. Decide which specialist roles to use from those available "
            "(match brief to their goals/tags).\n"
            "3. Delegate to specialists; on failure, replan and retry once.\n"
            f"4. When probing is done (or blocked), hand off to {analyzer_spec.id} "
            "for findings/report synthesis.\n"
            "Specialists must assert_in_scope before networked probes.\n"
        ),
        expected_output=(
            "A short engagement summary: what was attempted, key findings "
            "references, blockers, and confirmation that the Analyzer produced "
            "report notes."
        ),
    )
    analyze_task = Task(
        description=(
            "Build findings/report.md yourself from prior-agent workspace "
            "artifacts (list_workspace_artifacts + read_workspace_artifact) and "
            "list_findings. Inventory every concrete observation in those files. "
            "Do not run new probes. Do not report objective/job status. "
            "Write a findings report with write_report_note, one call "
            "per top-level section, in order: '1. Executive summary' (with "
            "overall risk rating); '2. Scope and methodology'; '3. Findings "
            "summary' (ID/severity/asset table); '4. Detailed findings' (one "
            "F-nn block per finding: description, evidence, impact, "
            "remediation); '5. Recommendations' (prioritized); 'Appendix A. "
            "Steps taken and commands executed'; 'Appendix B. Limitations'."
        ),
        expected_output=(
            "A pentest report with exactly those sections: executive summary "
            "and risk rating, scope/methodology, findings table, detailed "
            "findings with evidence and remediation, prioritized "
            "recommendations, and appendices — no process narrative."
        ),
        agent=analyzer,
        context=[engagement_task],
    )

    # Hierarchical crews: manager_agent is separate from agents list.
    # Crew-level planning only when the manager opts into advanced_reasoning.
    use_planning = bool(manager_spec.advanced_reasoning)
    kwargs: dict[str, Any] = {
        "agents": [*specialists, analyzer],
        "tasks": [engagement_task, analyze_task],
        "process": Process.hierarchical,
        "manager_agent": manager,
        "verbose": False,
    }
    if use_planning:
        kwargs["planning"] = True
        kwargs["planning_llm"] = llm_id_for_crew()
    try:
        return Crew(**kwargs)
    except TypeError:
        kwargs.pop("planning", None)
        kwargs.pop("planning_llm", None)
        try:
            return Crew(**kwargs)
        except TypeError:
            # Older crewai: manager must live in agents without manager_agent=
            kwargs.pop("manager_agent", None)
            kwargs["agents"] = [manager, *specialists, analyzer]
            return Crew(**kwargs)
