"""Minimal agent loop: gate → act ⇄ tools with iteration caps.

Plain control flow — no graph-execution framework. The system message is
synthesized fresh for every LLM call and never persisted into the running
message list, so it never leaks into the recover check (which looks at the
last *tool* message) or into the output snippets built from the tail of the
conversation at the end of a run.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from orchestrator.agent.config import AgentRunConfig
from orchestrator.agent.context import AgentRunContext, bind_job
from orchestrator.agent.policy import resolve_tool_names
from orchestrator.capabilities.registry import ensure_registered, get_tools_for_names
from orchestrator.config import get_config
from orchestrator.skills.misc.registry import SkillRegistry
from orchestrator.utils.llm import chat_model

_RECOVER_NUDGE = (
    "The last tool call failed. Adjust arguments or provision a missing CLI, "
    "then retry. Do not repeat the identical failing call."
)


def build_system_prompt(*, skill_names: list[str], brief: str) -> str:
    reg = SkillRegistry.shared()
    blocks: list[str] = [get_config().system_preamble.strip()]
    if brief.strip():
        blocks.append("Task brief:\n" + brief.strip()[:4000])
    for name in skill_names or []:
        skill = reg.load_skill(name)
        if skill is None:
            blocks.append(f"Skill {name!r}: (not in registry)")
            continue
        body = (skill.instructions or skill.description or "").strip()[:2500]
        blocks.append(f"## Skill: {skill.name}\n{body}")
    return "\n\n".join(blocks)


def _gate(ctx: AgentRunContext, messages: list[BaseMessage]) -> None:
    """Pull operator guidance from host ports before each turn; appends in place."""
    try:
        notes = list(ctx.ports.drain_operator_guidance() or [])
    except Exception as exc:  # noqa: BLE001 — surfaced as a host event, not raised
        ctx.ports.emit("error", f"operator guidance drain failed: {exc}")
        return
    notes = [n.strip() for n in notes if str(n or "").strip()]
    if not notes:
        return
    text = "\n\n".join(notes)
    ctx.ports.emit(
        "log",
        text[:2000],
        metadata={"event": "operator_guidance", "role": "assistant"},
    )
    messages.append(HumanMessage(content=text[:8000]))


def _run_tool_calls(tool_calls: list[dict[str, Any]], tools_by_name: dict[str, Any]) -> list[ToolMessage]:
    """Sequential tool execution — the loop's whole point is one call at a time, replanning on failure."""
    out: list[ToolMessage] = []
    for call in tool_calls:
        name = str(call.get("name") or "")
        call_id = str(call.get("id") or "")
        tool = tools_by_name.get(name)
        if tool is None:
            out.append(ToolMessage(content=f"Error: unknown tool {name!r}", tool_call_id=call_id, name=name))
            continue
        try:
            result = tool.invoke(call.get("args") or {})
        except Exception as exc:  # noqa: BLE001 — surfaced to the agent as a tool error, not raised
            result = f"Error: {exc}"
        out.append(ToolMessage(content=str(result), tool_call_id=call_id, name=name))
    return out


def invoke_agent(ctx: AgentRunContext, config: AgentRunConfig) -> tuple[bool, str, int]:
    """Run the agent loop under job context. Returns (ok, output, iterations)."""
    ensure_registered()
    names = resolve_tool_names(ctx.skill_names)
    tools = get_tools_for_names(names)
    tools_by_name = {t.name: t for t in tools}
    llm = chat_model()
    if tools:
        llm = llm.bind_tools(tools)
    system = build_system_prompt(skill_names=ctx.skill_names, brief=ctx.brief)
    max_iter = config.max_iterations
    max_replans = config.max_failure_replans

    human = (ctx.brief or f"Execute skills: {', '.join(ctx.skill_names)}").strip()
    messages: list[BaseMessage] = [HumanMessage(content=human[:8000])]
    iterations = 0
    failure_replans = 0

    with bind_job(ctx, config):
        while True:
            _gate(ctx, messages)

            resp = llm.invoke([SystemMessage(content=system), *messages])
            messages.append(resp)
            iterations += 1

            tool_calls = getattr(resp, "tool_calls", None) if isinstance(resp, AIMessage) else None
            if not tool_calls or iterations >= max_iter:
                break

            tool_messages = _run_tool_calls(tool_calls, tools_by_name)
            messages.extend(tool_messages)

            # recover: a tool result that looks like an error gets a nudge instead of
            # the loop silently repeating the same failing call, bounded by max_replans.
            last_content = str(getattr(tool_messages[-1], "content", "") or "") if tool_messages else ""
            if last_content.lower().startswith("error") and failure_replans < max_replans:
                messages.append(HumanMessage(content=_RECOVER_NUDGE))
                failure_replans += 1

    snippets: list[str] = []
    for m in messages[-8:]:
        content = getattr(m, "content", None)
        if isinstance(content, str) and content.strip():
            snippets.append(content.strip()[:2000])
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    snippets.append(str(part["text"])[:2000])
    text = "\n\n".join(snippets) if snippets else "(no agent text)"
    ok = iterations > 0
    return ok, text, iterations
