"""Role catalog DTOs + CrewAI Agent factory.

Roles come from the platform role store (id / kind) and are decorated from
affinity-bound skill packs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from orchestrator.config import get_config
from orchestrator.crew.tools import build_tools, with_collab_tools


@dataclass(frozen=True)
class RoleSpec:
    """CrewAI agent decoration + skill group."""

    id: str
    label: str
    crew_role: str
    goal: str
    backstory: str
    tools: tuple[str, ...] = ()
    allow_binaries: tuple[str, ...] = ()
    reports_to: str = ""
    capabilities: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    kind: str = "technical"  # engagement-pm | analyzer | technical
    allow_delegation: bool = False
    reasoning: bool = False
    advanced_reasoning: bool = False
    max_iter: int = 20

    @property
    def is_manager(self) -> bool:
        if self.kind == "engagement-pm":
            return True
        if self.kind in {"analyzer", "technical"}:
            return False
        return bool(self.allow_delegation) and not self.reports_to

    @property
    def is_analyzer(self) -> bool:
        if self.kind == "analyzer":
            return True
        # Explicit technical/PM kinds must not flip via skill capability tags
        # (e.g. a recon skill tagged "report").
        if self.kind in {"engagement-pm", "technical"}:
            return False
        caps = {c.lower() for c in self.capabilities}
        return "report" in caps or "analyzer" in caps

    @property
    def is_technical(self) -> bool:
        if self.kind == "technical":
            return True
        if self.kind in {"engagement-pm", "analyzer"}:
            return False
        return not self.is_manager and not self.is_analyzer


def llm_id_for_crew() -> str:
    """Map the platform LiteLLM model id into a CrewAI-friendly llm string."""
    cfg = get_config()
    model = (cfg.litellm_model or "").strip()
    return model or "openrouter/openai/gpt-5.6-luna"


def build_crew_agent(
    role: RoleSpec,
    *,
    tools: list[Any] | None = None,
    skill_ids: Iterable[str] | None = None,
    max_iterations: int | None = None,
    reasoning_effort: str | None = None,
    reasoning_max_attempts: int | None = None,
    collaborate: bool = False,
    dispatch_only: bool = False,
) -> Any:
    """Instantiate a CrewAI ``Agent`` for ``role`` (lazy crewai import).

    ``collaborate`` forces ``allow_delegation`` on, so CrewAI injects its native
    "Ask question to coworker" / "Delegate work to coworker" tools and the agent
    can talk to its crew peers directly (used for in-process engagement crews).
    """
    try:
        from crewai import Agent
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "crewai is required for AGENT_MODULE=crewai "
            "(pip install 'crewai>=1.0.0')"
        ) from exc

    from orchestrator.skills.bind import (
        effective_tools,
        progressive_tool_names,
        resolve_skill_ids,
        skills_prompt_block,
    )

    selected = resolve_skill_ids(role, skill_ids)
    backstory = role.backstory
    skills_block = skills_prompt_block(selected, role_id=role.id)
    if skills_block:
        backstory = f"{backstory}\n\n{skills_block}".strip()
    # MCP Crew tools are technical-only; keep the prompt aligned.
    if role.is_technical or role.kind == "technical":
        try:
            from orchestrator.mcp.provision import mcp_prompt_block

            mcp_block = mcp_prompt_block(role.id)
        except Exception:
            mcp_block = ""
        if mcp_block:
            backstory = f"{backstory}\n\n{mcp_block}".strip()
    if role.reports_to:
        backstory = (
            f"{backstory}\n\n## Hierarchy\nYou report to `{role.reports_to}`."
        ).strip()
    if role.is_technical or role.is_manager:
        # CrewAI-native coworker tools exist only in the in-process crew
        # (``collaborate``). In the job model peers are reached via A2A
        # (see "Working with the other agents"), so only instruct coworker
        # delegation when those tools are actually present.
        coworker_hint = (
            "Talk to your crew peers directly: use **Ask question to coworker** "
            "to get another specialist's input and **Delegate work to coworker** "
            "to hand a peer a focused sub-task — prefer these over working blind "
            "or escalating. "
            if collaborate
            else ""
        )
        solo_rule = (
            ""
            if dispatch_only
            else (
                "Solo in-process `bash` is only for a single host/URL or a quick "
                "verify — **never** for a CIDR/subnet, a host or target list, or "
                "anything you'd otherwise describe as a sweep/enumeration; those "
                "always go through `spawn_work` (slice the range across children) "
                "even when one tool invocation could cover the whole range in a "
                "single command. "
            )
        )
        backstory = (
            f"{backstory}\n\n## Collaboration\n"
            f"{coworker_hint}"
            "Engagement PM owns the objective bookend; you govern "
            "**in-lane** work and **must parallelize** heavy fan-out via Dramatiq "
            "child jobs: for multi-host/CIDR/target lists or multi-skill "
            "follow-ons use `spawn_work` (empty `role_id` = this expert) with "
            "focused `skill_ids` + `scope_slice`, then `go_idle` until they "
            "report back (a child's result wakes you) before synthesizing. Do "
            "**not** ask the PM to replan for follow-ups "
            "already in your skill group. You are a specialized PM: your job is "
            "to make sure in-lane objectives are fulfilled, mainly by spawning "
            "and directing workers, not by running the scan/probe yourself. "
            f"{solo_rule}"
            "Out-of-lane work, unknown roles, "
            "or scope rules ambiguity: `list_roles` then spawn another catalog role, or "
            "`ask_operator`. Never invent role or skill ids."
            "\n\n## Working with the other agents\n"
            "Every planned agent already exists; ones waiting on dependencies are "
            "**idle** and wake when a message arrives. `list_team` shows who is "
            "who. When you finish work another agent needs, or need something "
            "from one, `send_message` it with the detail in the message (what "
            "you found, exact paths/targets, what you want back) — it will pick "
            "the work up. Check `check_inbox` between steps. If you have done "
            "all you can and need another agent's output, call `go_idle` and end "
            "your turn with a short summary of what is done and what you wait "
            "for — never poll or sleep-loop. Use `spawn_work` only when no "
            "suitable agent exists."
            "\n\n## Memory\n"
            "Your brief starts with a memory index (this engagement + platform "
            "lessons). Read it before acting; `recall_memory` pulls detail — "
            "query it before trying a method, tool or target class that may "
            "have been tried. Record what a later agent would otherwise "
            "rediscover with `save_memory`: durable facts about the target, "
            "tool behaviours that held or failed (generic ones as scope "
            "`platform`, with no targets/IPs/emails/URLs), and the reason "
            "behind decisions. Operator feedback overrides your own "
            "preference. Retire notes you find wrong with `forget_memory`."
        ).strip()
    if dispatch_only and tools is None:
        backstory = (
            f"{backstory}\n\n## Dispatch mode\n"
            "You are the lane supervisor for this objective: you do **not** "
            "execute commands, scripts or tool calls yourself — those tools are "
            "not available to you. Break the objective into focused units and "
            "`spawn_work` a worker for each (leave `role_id` empty to reuse this "
            "role) with the `skill_ids` that unit needs and a precise brief "
            "(targets, expected output path). Then `go_idle`; each worker's "
            "result wakes you. Inspect results with `list_findings`, "
            "`list_workspace_artifacts` and `read_workspace_artifact`, verify "
            "the objective's acceptance criteria, spawn follow-ups only for "
            "real gaps, and persist your lane's deliverable yourself with "
            "`write_workspace_artifact` (never spawn a job just to write or "
            "merge a file). Workers stay in this objective's skills. Close "
            "with a concise synthesis; the objective completes when you end "
            "your turn."
        ).strip()
    if role.is_manager:
        backstory = (
            f"{backstory}\n\n## Memory — manager duties\n"
            "You own the engagement's memory. At the start of every run read the "
            "full-text 'Standing instructions & decisions' in your index and "
            "plan within them. Before any replan, `recall_memory` the failures "
            "and dead ends of this engagement and platform lessons for the "
            "approach you are about to choose. When an objective completes or "
            "fails, record the decision and its reason (`decision`), any "
            "operator correction you received (`feedback`), and generic "
            "lessons for future engagements (`lesson`, scope `platform`). "
            "Keep the index tidy: update by reusing the name, `forget_memory` "
            "what turned out wrong."
        ).strip()

    cfg = get_config()
    role_iters = max(1, int(role.max_iter))
    cap = max(
        1,
        int(
            max_iterations
            if max_iterations is not None
            else cfg.agent_max_iterations
        ),
    )
    tool_names = effective_tools(role, selected) if tools is None else None
    if tools is not None:
        agent_tools = tools
    else:
        names = list(with_collab_tools(role, tool_names=tool_names))
        for extra in progressive_tool_names(selected, role_id=role.id):
            if extra not in names:
                names.append(extra)
        if dispatch_only:
            from orchestrator.crew.tools.catalog import for_dispatch_only

            names = list(for_dispatch_only(names))
        agent_tools = build_tools(names)
    kwargs: dict[str, Any] = {
        "role": role.crew_role,
        "goal": role.goal,
        "backstory": backstory,
        "tools": agent_tools,
        "allow_delegation": bool(role.allow_delegation or role.is_manager or collaborate),
        "verbose": False,
        "max_iter": min(role_iters, cap),
        "llm": llm_id_for_crew(),
    }
    if role.reasoning:
        effort = (
            (reasoning_effort or cfg.crew_reasoning_effort or "low").strip().lower()
        )
        if effort not in {"low", "medium", "high"}:
            effort = "low"
        attempts = max(
            1,
            min(
                5,
                int(
                    reasoning_max_attempts
                    if reasoning_max_attempts is not None
                    else cfg.crew_reasoning_max_attempts
                ),
            ),
        )
        try:
            from crewai.agent.planning_config import PlanningConfig

            kwargs["planning_config"] = PlanningConfig(
                reasoning_effort=effort,
                max_attempts=attempts,
            )
        except Exception:
            kwargs["reasoning"] = True
            kwargs["max_reasoning_attempts"] = attempts
    try:
        return Agent(**kwargs)
    except TypeError:
        kwargs.pop("planning_config", None)
        kwargs.pop("reasoning", None)
        kwargs.pop("max_reasoning_attempts", None)
        return Agent(**kwargs)
