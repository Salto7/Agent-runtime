"""Project-level CrewAI runtime (hierarchical manager crew)."""

from __future__ import annotations

import uuid
from typing import Any

from orchestrator.agent.config import AgentRunConfig
from orchestrator.agent.job import JobScope, bind_job
from orchestrator.crew.flows.engagement import build_engagement_crew
from orchestrator.crew.roles.registry import RoleRegistry
from orchestrator.crew.runtime_base import (
    CrewRunRequest,
    CrewRunResult,
    CrewRuntimeBase,
)
from orchestrator.utils import llm_usage
from orchestrator.utils.stream_events import envelope


class ProjectCrewRuntime(CrewRuntimeBase):
    """Run manager → specialists → analyzer for one project."""

    def start(self, request: CrewRunRequest) -> CrewRunResult:
        meta = dict(request.metadata or {})
        scope: JobScope | None = meta.get("scope")
        config: AgentRunConfig | None = meta.get("config")
        if scope is None:
            return CrewRunResult(
                ok=False,
                error="ProjectCrewRuntime requires metadata['scope'] (JobScope)",
                status="stopped",
                flow_id=request.flow_id,
            )
        cfg = config or AgentRunConfig()
        if not cfg.runtime_enabled:
            return CrewRunResult(
                ok=False,
                error="agent runtime disabled (AGENT_RUNTIME_ENABLED=false)",
                status="stopped",
                flow_id=request.flow_id,
            )

        flow_id = (request.flow_id or "").strip() or str(uuid.uuid4())
        brief = (request.brief or scope.brief or "").strip()
        if request.steer:
            brief = f"{brief}\n\nOPERATOR INSTRUCTION:\n{request.steer.strip()}".strip()

        scope.extras = dict(scope.extras or {})
        mgr = RoleRegistry.shared().manager()
        if mgr:
            scope.extras["role_id"] = mgr.id
        scope.extras["crew_flow_id"] = flow_id
        scope.extras["crew_mode"] = "project"

        try:
            crew = build_engagement_crew(
                brief=brief,
                role_ids=request.role_ids,
                replan_note=request.steer if request.replan else "",
                config=cfg,
            )
        except Exception as exc:
            scope.bridge.emit("error", f"crew build failed: {exc}")
            return CrewRunResult(
                ok=False,
                error=str(exc),
                flow_id=flow_id,
                status="stopped",
            )

        action = "replan" if request.replan else ("resume" if request.resume else "start")
        scope.bridge.emit(
            "status",
            f"project crew {action} flow={flow_id[:8]}",
            metadata=envelope(
                "crew_project_start",
                {"flow_id": flow_id, "action": action},
            ),
        )

        with bind_job(scope, cfg), llm_usage.purpose("agent_loop_crew"):
            try:
                result = crew.kickoff()
            except Exception as exc:
                scope.bridge.emit("error", f"project crew failed: {exc}")
                return CrewRunResult(
                    ok=False,
                    error=str(exc),
                    flow_id=flow_id,
                    status="stopped",
                )
            finally:
                agents = [*(getattr(crew, "agents", None) or []), getattr(crew, "manager_agent", None)]
                llm_usage.record_crew_usage([getattr(a, "llm", None) for a in agents if a is not None])

        output = _crew_output(result)
        scope.bridge.emit(
            "status",
            f"project crew done flow={flow_id[:8]}",
            metadata=envelope("crew_project_done", {"flow_id": flow_id}),
        )
        return CrewRunResult(
            ok=True,
            output=output,
            flow_id=flow_id,
            status="done",
            metadata={"action": action},
        )


def _crew_output(result: Any) -> str:
    if result is None:
        return ""
    raw = getattr(result, "raw", None)
    if raw is not None:
        return str(raw)
    return str(result)


def run_project_crew_from_scope(
    scope: JobScope,
    config: AgentRunConfig | None = None,
    *,
    role_ids: tuple[str, ...] = (),
    resume: bool = False,
    replan: bool = False,
    steer: str = "",
    flow_id: str = "",
) -> CrewRunResult:
    """Helper used by the job runtime when the manager role owns the project."""
    runtime = ProjectCrewRuntime()
    return runtime.start(
        CrewRunRequest(
            project_id=scope.project_id,
            brief=scope.brief,
            role_ids=role_ids,
            resume=resume,
            replan=replan,
            steer=steer,
            flow_id=flow_id or str(scope.extras.get("crew_flow_id") or ""),
            metadata={"scope": scope, "config": config or AgentRunConfig()},
        )
    )
