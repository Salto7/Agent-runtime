"""Named CrewAI tools selectable from role/skill tool lists."""

from __future__ import annotations

from typing import Any, Callable, Iterable

ToolFactory = Callable[[], Any]

_FACTORIES: dict[str, ToolFactory] = {}

# Engagement roles get these without listing them on every skill pack.
# In the job model (one role per Dramatiq Job — the default) peer messaging is
# Peon A2A: send_message / check_inbox / go_idle wake idle agents across jobs.
# CrewAI's native "Ask/Delegate to coworker" tools apply only to the in-process
# crew (crew_mode=project), where several agents share one Crew.
COLLAB_TOOLS: tuple[str, ...] = (
    "list_roles",
    "spawn_work",
    "wait_for_work",
    "list_work",
    "propose_work",
    "ask_operator",
    # Persistent memory (index is injected at start; these fetch/record detail).
    "recall_memory",
    "save_memory",
    "forget_memory",
    # A2A: address other agents directly; idle agents wake on messages.
    "list_team",
    "send_message",
    "check_inbox",
    "message_status",
    "go_idle",
)
MANAGER_COLLAB_TOOLS: tuple[str, ...] = (*COLLAB_TOOLS, "list_delegations")

# CrewAI sandbox / findings surface (skills often declare OpenCode-only names).
SANDBOX_RUNTIME_TOOLS: tuple[str, ...] = (
    "sandbox_status",
    "provision_cli",
    "bash",
    "run_periodic",
    "roe_status",
    "assert_in_scope",
)
FINDINGS_RUNTIME_TOOLS: tuple[str, ...] = (
    "record_finding",
    "record_findings",
    "record_assets",
    "list_findings",
)
OBJECTIVE_RUNTIME_TOOLS: tuple[str, ...] = (
    "list_objectives",
    "update_objective_status",
)
ANALYZER_RUNTIME_TOOLS: tuple[str, ...] = (
    *OBJECTIVE_RUNTIME_TOOLS,
    *FINDINGS_RUNTIME_TOOLS,
    "list_team",
    "send_message",
    "check_inbox",
    "message_status",
    "recall_memory",
    "save_memory",
    "forget_memory",
    "list_workspace_artifacts",
    "read_workspace_artifact",
    "write_report_note",
)
TECHNICAL_RUNTIME_TOOLS: tuple[str, ...] = (
    *SANDBOX_RUNTIME_TOOLS,
    *OBJECTIVE_RUNTIME_TOOLS,
    *FINDINGS_RUNTIME_TOOLS,
    "list_mcp_tools",
    "call_mcp_tool",
    "list_mcp_resources",
    "read_mcp_resource",
)
MANAGER_RUNTIME_TOOLS: tuple[str, ...] = (
    *SANDBOX_RUNTIME_TOOLS,
    *OBJECTIVE_RUNTIME_TOOLS,
    "list_findings",
)

# Tools that execute commands / external calls. A dispatch-only supervisor
# (a specialist job with no parent) does not get these: it spawns a worker.
EXECUTION_TOOLS: frozenset[str] = frozenset(
    {"bash", "run_skill_script", "run_periodic", "provision_cli", "call_mcp_tool"}
)
# What a supervisor needs to read the outcome of the work it dispatched.
SUPERVISOR_INSPECTION_TOOLS: tuple[str, ...] = (
    "list_workspace_artifacts",
    "read_workspace_artifact",
    "write_workspace_artifact",
)


def for_dispatch_only(names: Iterable[str]) -> tuple[str, ...]:
    """Tool names for a supervisor: no execution tools, plus result inspection."""
    kept = [n for n in names if n not in EXECUTION_TOOLS]
    for extra in SUPERVISOR_INSPECTION_TOOLS:
        if extra not in kept:
            kept.append(extra)
    return tuple(kept)


# Older skill-runtime names → CrewAI catalog names. ``None`` drops the token.
SKILL_TOOL_ALIASES: dict[str, str | None] = {
    "spawn_agent": "spawn_work",
    "wait_for_agents": "wait_for_work",
    "propose_agents": "propose_work",
    "list_agents": "list_work",
    "sandbox_setup": "sandbox_status",
    "skills_list": None,
    "skill_view": None,
}


def register_tool(name: str, factory: ToolFactory) -> ToolFactory:
    key = (name or "").strip()
    if not key:
        raise ValueError("tool name required")
    _FACTORIES[key] = factory
    return factory


def crew_tool(name: str, description: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a function as a named CrewAI tool factory."""

    def deco(fn: Callable[..., str]) -> Callable[..., str]:
        if description and not (fn.__doc__ or "").strip():
            fn.__doc__ = description

        def factory() -> Any:
            try:
                # deferred: optional heavy crewai
                from crewai.tools import tool as crewai_tool
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "crewai is required for AGENT_MODULE=crewai "
                    "(pip install 'crewai>=1.0.0')"
                ) from exc

            wrapped = crewai_tool(name)(fn)
            if description and hasattr(wrapped, "description"):
                try:
                    wrapped.description = description
                except Exception:
                    pass
            return wrapped

        register_tool(name, factory)
        return fn

    return deco


def known_tool_names() -> list[str]:
    _ensure()
    return sorted(_FACTORIES)


def canonicalize_crew_tools(names: Iterable[str] | None) -> tuple[str, ...]:
    """Map skill OpenCode tool names onto CrewAI tools; drop non-crew names."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in names or ():
        name = str(raw or "").strip()
        if not name:
            continue
        if name in SKILL_TOOL_ALIASES:
            mapped = SKILL_TOOL_ALIASES[name]
            if mapped is None:
                continue
            name = mapped
        if name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _extend(base: list[str], seen: set[str], extra: Iterable[str]) -> None:
    for name in extra:
        if name not in seen:
            base.append(name)
            seen.add(name)


def with_collab_tools(
    role: Any,
    *,
    tool_names: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Role/skill tools plus Peon runtime + lane-orchestration tools.

    Skills often list OpenCode-only names; Peon injects the CrewAI sandbox /
    findings / objective surface so specialists can actually execute.
    """
    raw = tool_names if tool_names is not None else getattr(role, "tools", ()) or ()
    base = list(canonicalize_crew_tools(raw))
    seen = set(base)

    if getattr(role, "is_analyzer", False) or getattr(role, "kind", "") == "analyzer":
        _extend(base, seen, ANALYZER_RUNTIME_TOOLS)
        return tuple(base)

    if getattr(role, "is_manager", False) or getattr(role, "kind", "") == "engagement-pm":
        _extend(base, seen, MANAGER_RUNTIME_TOOLS)
        _extend(base, seen, MANAGER_COLLAB_TOOLS)
        return tuple(base)

    if getattr(role, "is_technical", False) or getattr(role, "kind", "") == "technical":
        _extend(base, seen, TECHNICAL_RUNTIME_TOOLS)
        _extend(base, seen, COLLAB_TOOLS)
        return tuple(base)

    return tuple(base)


# Real progressive tools. They are not OpenCode aliases and must not be remapped.
PROGRESSIVE_DIRECT_TOOLS = frozenset(
    {"load_skill", "read_skill_resource", "run_skill_script"}
)


def partition_tool_names(
    names: Iterable[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Keep progressive tools intact; alias the rest through the OpenCode map."""
    direct: list[str] = []
    rest: list[str] = []
    seen_direct: set[str] = set()
    for raw in names or ():
        name = str(raw or "").strip()
        if not name:
            continue
        if name in PROGRESSIVE_DIRECT_TOOLS:
            if name not in seen_direct:
                seen_direct.add(name)
                direct.append(name)
            continue
        rest.append(name)
    return tuple(direct), canonicalize_crew_tools(rest)


def build_tools(names: Iterable[str]) -> list[Any]:
    """Instantiate allowlisted tools; unknown names raise."""
    _ensure()
    direct, aliased = partition_tool_names(names)
    out: list[Any] = []
    seen: set[str] = set()
    for raw in (*aliased, *direct):
        if raw in seen:
            continue
        seen.add(raw)
        factory = _FACTORIES.get(raw)
        if factory is None:
            known = ", ".join(known_tool_names()) or "(none)"
            raise KeyError(f"unknown role tool {raw!r}; known: {known}")
        out.append(factory())
    return out


def _ensure() -> None:
    if _FACTORIES:
        return
    # circular: adapters → crew_tool from this module
    from orchestrator.crew.tools import adapters as _adapters  # noqa: F401
