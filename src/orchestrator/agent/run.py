"""Entry point for one Job agent run."""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.agent.config import AgentRunConfig
from orchestrator.agent.context import AgentRunContext


@dataclass(frozen=True)
class AgentRunResult:
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    replans: int = 0


def run_agent(ctx: AgentRunContext, config: AgentRunConfig) -> AgentRunResult:
    """Run the agent loop for ``ctx.job_id`` (sandbox must already be bound)."""
    if not config.runtime_enabled:
        return AgentRunResult(
            ok=False,
            error="agent runtime disabled (AGENT_RUNTIME_ENABLED=false)",
        )
    if not (ctx.skill_names or ctx.brief.strip()):
        return AgentRunResult(ok=False, error="no skill_names or brief on context")

    try:
        from orchestrator.agent.graph import invoke_agent

        ok, output, iterations = invoke_agent(ctx, config)
    except Exception as exc:
        ctx.ports.emit("error", f"agent failed: {exc}")
        return AgentRunResult(ok=False, error=str(exc), output="")

    ctx.ports.emit(
        "status",
        f"agent finished ok={ok} iterations={iterations}",
        metadata={"event": "agent_done", "iterations": iterations},
    )
    return AgentRunResult(ok=ok, output=output, iterations=iterations)
