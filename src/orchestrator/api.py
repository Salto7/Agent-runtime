"""High-level facade: list / suggest / plan / run (Docker sandbox)."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.agent.config import AgentRunConfig
from orchestrator.agent.context import AgentPorts, AgentRunContext, NullAgentPorts
from orchestrator.agent.run import AgentRunResult, run_agent
from orchestrator.config import RuntimeConfig, bootstrap
from orchestrator.sandbox.provision import SandboxProvisioner
from orchestrator.skills.execute import LocalSkillExecutor, SkillExecutionDispatcher
from orchestrator.skills.misc.catalog import filter_skills
from orchestrator.skills.misc.matcher import SkillNameMatcher
from orchestrator.skills.misc.registry import SkillRegistry
from orchestrator.skills.misc.router import SkillRouter
from orchestrator.tools.catalog import ToolCatalog
from orchestrator.utils.job_env import JobEnv
from orchestrator.utils.llm import chat_text, llm_config

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = (
    "You are a planning assistant for a Docker-sandbox agent runtime.\n"
    "Given a user goal and available skills/tools, write a concise execution plan.\n"
    "Do NOT run tools. Output markdown with:\n"
    "1) Goal\n"
    "2) Selected skills (from the provided catalog only)\n"
    "3) Ordered steps\n"
    "4) CLI tools likely needed\n"
    "Keep it short and actionable."
)


def _resolve_skills(text: str) -> list[str]:
    """Pick skills for a prompt: LLM router when keyed, else name-match only."""
    if (llm_config().api_key or "").strip():
        return SkillRouter.resolve_default_skills(text)
    valid = {
        s.name
        for s in SkillRegistry.shared().get_registry().values()
        if s.jobable
    }
    return SkillNameMatcher.find(text, valid)


class LoggingPorts(NullAgentPorts):
    """Emit agent events to stdout / logger; optional callback."""
    __slots__ = ("_on_event", "events")

    def __init__(self, on_event: Any | None = None) -> None:
        self._on_event = on_event
        self.events: list[dict[str, Any]] = []

    def emit(
        self, message_type: str, content: str, *, metadata: dict[str, Any] | None = None
    ) -> None:
        event = {
            "type": message_type,
            "content": content,
            "metadata": metadata or {},
        }
        self.events.append(event)
        if self._on_event:
            self._on_event(event)
        else:
            logger.info("[%s] %s", message_type, content[:500])


@dataclass(slots=True)
class OrchestratorResult:
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    session_id: str = ""
    sandbox_name: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    plan: str = ""
    skills: list[str] = field(default_factory=list)

    @classmethod
    def from_agent(
        cls,
        result: AgentRunResult,
        *,
        session_id: str,
        sandbox_name: str = "",
        events: list[dict[str, Any]] | None = None,
        plan: str = "",
        skills: list[str] | None = None,
    ) -> OrchestratorResult:
        return cls(
            ok=result.ok,
            output=result.output,
            error=result.error,
            iterations=result.iterations,
            session_id=session_id,
            sandbox_name=sandbox_name,
            events=list(events or []),
            plan=plan,
            skills=list(skills or []),
        )


@dataclass(slots=True)
class PlanResult:
    ok: bool
    plan: str = ""
    skills: list[str] = field(default_factory=list)
    error: str = ""


class Orchestrator:
    """Zero-setup litellm + Docker sandbox orchestrator."""

    def __init__(
        self,
        root: Path | str | None = None,
        config: RuntimeConfig | None = None,
        *,
        ports: AgentPorts | None = None,
        on_event: Any | None = None,
        **config_overrides: object,
    ) -> None:
        from orchestrator.config import configure
        self.config = configure(config, **config_overrides) if config is not None else bootstrap(root, **config_overrides)
        self._ports = ports
        self._on_event = on_event
        self._ensure_executors()

    def _ensure_executors(self) -> None:
        dispatcher = SkillExecutionDispatcher.shared()
        if not any(isinstance(e, LocalSkillExecutor) for e in dispatcher._executors):
            dispatcher.register(LocalSkillExecutor.shared())

    def _agent_config(self) -> AgentRunConfig:
        cfg = self.config
        return AgentRunConfig(
            max_failure_replans=cfg.agent_max_failure_replans,
            max_iterations=cfg.agent_max_iterations,
            max_subagents=cfg.agent_max_subagents,
            max_subagent_depth=cfg.agent_max_subagent_depth,
            runtime_enabled=cfg.agent_runtime_enabled,
        )

    # Catalog

    def list_skills(
        self, *, jobable_only: bool = False, include_platform: bool = True
    ) -> list[dict[str, Any]]:
        skills = list(SkillRegistry.shared().get_registry().values())
        if jobable_only:
            skills = filter_skills(skills, jobable_only=True)
        elif not include_platform:
            skills = [s for s in skills if (s.category or "") != "platform"]
        return [
            {
                "name": s.name,
                "description": s.description,
                "category": s.category or "",
                "tags": list(s.tags or []),
                "toolkit": list(s.toolkit or []),
                "jobable": bool(s.jobable),
                "lifecycle": s.lifecycle or "",
            }
            for s in sorted(skills, key=lambda x: x.name)
        ]

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "id": tool.id,
                "name": tool.name,
                "binary": tool.binary or tool.id,
                "description": (tool.description or "").strip(),
                "skills": list(tool.skills or []),
                "tags": list(tool.tags or []),
            }
            for tool in sorted(ToolCatalog.shared().all().values(), key=lambda t: t.id)
            if not tool.is_image_tier
        ]

    def suggest_skills(self, prompt: str) -> dict[str, Any]:
        """Recommend existing catalog skills for a prompt (router, optional LLM)."""
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt is required")
        names = SkillRouter.resolve_default_skills(text)
        reg = SkillRegistry.shared()
        details = [
            {
                "name": skill.name,
                "description": skill.description,
                "category": skill.category or "",
                "toolkit": list(skill.toolkit or []),
            }
            for name in names
            if (skill := reg.load_skill(name)) is not None
        ]
        return {"skills": names, "details": details, "prompt": text}

    # Plan / Run

    def plan(self, prompt: str, *, skill_names: list[str] | None = None) -> PlanResult:
        """Plan only — no sandbox execution."""
        text = (prompt or "").strip()
        if not text:
            return PlanResult(ok=False, error="prompt is required")
        try:
            skills = list(skill_names or []) or _resolve_skills(text)
            has_llm = bool((llm_config().api_key or "").strip())

            if not has_llm:
                # Deterministic fallback without LLM
                skill_lines = [f"- {n}" for n in skills] or ["- (none)"]
                lines = [
                    "# Plan",
                    "",
                    f"## Goal\n{text}",
                    "",
                    "## Selected skills",
                    *skill_lines,
                    "",
                    "## Steps",
                    "1. Provision Docker sandbox",
                    "2. Bind selected skills",
                    "3. Execute via agent tool loop",
                ]
                return PlanResult(ok=True, plan="\n".join(lines), skills=skills)

            catalog = "\n".join(
                f"- {s['name']}: {s['description'][:160]}"
                for s in self.list_skills(jobable_only=True)
            )
            tools = "\n".join(
                f"- {t['id']} ({t['binary']}): {t['description'][:120]}"
                for t in self.list_tools()[:40]
            )
            human = (
                f"User goal:\n{text}\n\n"
                f"Preselected skills: {', '.join(skills) or '(none)'}\n\n"
                f"Skill catalog:\n{catalog or '(empty)'}\n\n"
                f"Tool catalog:\n{tools or '(empty)'}\n"
            )
            plan_text = chat_text(_PLAN_SYSTEM, human)
            return PlanResult(ok=True, plan=plan_text.strip(), skills=skills)
        except Exception as exc:
            logger.exception("plan failed")
            return PlanResult(ok=False, error=str(exc))

    def run(
        self,
        prompt: str,
        *,
        skill_names: list[str] | None = None,
        session_id: str | None = None,
        workspace: Path | str | None = None,
        remove_sandbox: bool | None = None,
        auto_skills: bool = True,
        plan_first: bool = False,
    ) -> OrchestratorResult:
        """Provision a sandbox and run the agent on ``prompt``."""
        brief = (prompt or "").strip()
        skills = list(skill_names or [])
        if not skills and auto_skills and brief:
            try:
                skills = _resolve_skills(brief)
            except Exception:
                skills = []
        if not brief and not skills:
            return OrchestratorResult(ok=False, error="prompt or skill_names required")

        plan_text = ""
        if plan_first and brief:
            planned = self.plan(brief, skill_names=skills)
            plan_text = planned.plan
            if planned.skills and not skill_names:
                skills = planned.skills

        sid = (session_id or uuid.uuid4().hex[:12]).strip()
        ws = Path(workspace) if workspace else (self.config.workspaces_dir / sid)
        ws = ws.resolve()
        ws.mkdir(parents=True, exist_ok=True)
        if plan_text:
            plans = ws / "plans"
            plans.mkdir(parents=True, exist_ok=True)
            (plans / "latest.md").write_text(plan_text + "\n", encoding="utf-8")

        ports = self._ports or LoggingPorts(on_event=self._on_event)
        provisioner = SandboxProvisioner(config=self.config)
        sandbox_name = ""
        socks_dir = ws / ".orchestrator-sockets"
        socks_dir.mkdir(parents=True, exist_ok=True)
        rpc_path = str((socks_dir / "rpc.sock").resolve())
        stream_path = str((socks_dir / "stream.sock").resolve())

        from orchestrator.runtime.rpc import RpcServer, close_rpc_server
        from orchestrator.runtime.stream_server import StreamSocketServer

        def _on_stream(payload: dict[str, Any]) -> None:
            ports.emit(
                str(payload.get("message_type") or "log"),
                str(payload.get("content") or ""),
                metadata=dict(payload.get("metadata") or {}),
            )

        rpc: RpcServer | None = None
        stream_srv: StreamSocketServer | None = None
        try:
            rpc = RpcServer.start(
                rpc_path,
                job_id=sid,
                on_call=lambda tool, args, meta: ports.emit(
                    "tool",
                    f"[rpc] {tool}({json.dumps(args)[:200]})",
                    metadata={"rpc": True, "tool": tool, **meta},
                ),
            )
            stream_srv = StreamSocketServer(stream_path, _on_stream)
            stream_srv.start()
        except OSError as exc:
            logger.warning("host bridge sockets unavailable: %s", exc)
            close_rpc_server(rpc)
            rpc = None
            if stream_srv is not None:
                stream_srv.stop()
                stream_srv = None

        env_map = {
            "ORCHESTRATOR_JOB_ID": sid,
            "ORCHESTRATOR_PROJECT_ID": sid,
            "ORCHESTRATOR_JOB_BRIEF": brief[:4000],
            "ORCHESTRATOR_WORKSPACE": str(ws),
            "PROJECT_WORKSPACES_DIR": str(self.config.workspaces_dir),
            "ORCHESTRATOR_SOCKETS_DIR": str(socks_dir.resolve()),
        }
        if rpc is not None:
            env_map["ORCHESTRATOR_RPC_SOCKET"] = rpc_path
            env_map["ORCHESTRATOR_RPC_TOKEN"] = rpc.token
        if stream_srv is not None:
            env_map["ORCHESTRATOR_STREAM_SOCKET"] = stream_path

        job_token = JobEnv.bind(env_map)
        try:
            info = provisioner.provision(
                sid,
                workspace=ws,
                skills_dir=self.config.skills_dir,
                tools_dir=self.config.tools_catalog_dir,
            )
            sandbox_name = info.name
            ports.emit(
                "status",
                f"sandbox {info.action}: {info.name} mode={info.mode}",
                metadata={"event": "sandbox_ready", "name": info.name},
            )
            ctx = AgentRunContext(
                job_id=sid,
                project_id=sid,
                workspace=str(ws),
                skill_names=skills,
                brief=brief or plan_text,
                ports=ports,
            )
            result = run_agent(ctx, self._agent_config())
            events = getattr(ports, "events", [])
            return OrchestratorResult.from_agent(
                result,
                session_id=sid,
                sandbox_name=sandbox_name,
                events=events,
                plan=plan_text,
                skills=skills,
            )
        except Exception as exc:
            logger.exception("orchestrator run failed")
            ports.emit("error", str(exc))
            events = getattr(ports, "events", [])
            return OrchestratorResult(
                ok=False,
                error=str(exc),
                session_id=sid,
                sandbox_name=sandbox_name,
                events=events,
                plan=plan_text,
                skills=skills,
            )
        finally:
            JobEnv.reset(job_token)
            close_rpc_server(rpc)
            if stream_srv is not None:
                stream_srv.stop()
            should_remove = (
                self.config.sandbox_remove_on_exit
                if remove_sandbox is None
                else remove_sandbox
            )
            if should_remove:
                try:
                    provisioner.remove(sid)
                except Exception as exc:
                    logger.warning("sandbox cleanup failed: %s", exc)


def run(prompt: str, **kwargs: Any) -> OrchestratorResult:
    """Module-level convenience wrapper around :class:`Orchestrator`."""
    return Orchestrator().run(prompt, **kwargs)
