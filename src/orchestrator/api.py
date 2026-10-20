"""High-level facade: catalog, plan, run (single role or whole crew), dream.

``Orchestrator`` wires the library together for a host that has no control plane
of its own: configuration, the platform catalog (roles / skills / MCP), the
LiteLLM provider, the CrewAI job + project runtimes, the per-project sandbox,
A2A, memory and dreaming. Hosts with their own job store pass a different
``plane`` (see ``orchestrator.host``); everything else stays the same.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from orchestrator import settings
from orchestrator.catalog import PlatformCatalog
from orchestrator.config import RuntimeConfig, bootstrap, configure
from orchestrator.crew.roles.registry import RoleRegistry
from orchestrator.crew.router import RoleRouter
from orchestrator.host.control import LocalControlPlane
from orchestrator.host.state import JobState, Roe
from orchestrator.skills.registry import SkillRegistry
from orchestrator.host.state import TERMINAL
from orchestrator.tools.catalog import ToolCatalog
from orchestrator.utils.llm import llm_configured

logger = logging.getLogger(__name__)

EventHook = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class OrchestratorResult:
    ok: bool
    output: str = ""
    error: str = ""
    project_id: str = ""
    job_id: str = ""
    status: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    plan: str = ""
    role_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_job(cls, job: JobState, *, plan: str = "") -> "OrchestratorResult":
        return cls(
            ok=job.status == "completed",
            output=job.result,
            error=job.error,
            project_id=job.project_id,
            job_id=job.id,
            status=job.status,
            events=list(job.events),
            plan=plan,
            role_ids=list(job.role_ids),
        )


@dataclass(slots=True)
class PlanResult:
    ok: bool
    plan: str = ""
    role_ids: list[str] = field(default_factory=list)
    objectives: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


class Orchestrator:
    """Configure once, then ``plan`` / ``run`` / ``run_project`` / ``dream``.

    ``executor="thread"`` (default) runs jobs in this process; ``executor="dramatiq"`` (or
    ``ORCHESTRATOR_EXECUTOR=dramatiq``) enqueues them to workers with state shared through Redis.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        config: RuntimeConfig | None = None,
        *,
        on_event: EventHook | None = None,
        operator: Callable[[dict[str, Any]], str] | None = None,
        sandbox: bool = True,
        plane: LocalControlPlane | None = None,
        executor: str | None = None,
        **config_overrides: object,
    ) -> None:
        self.config = (
            configure(config, **config_overrides)
            if config is not None
            else bootstrap(root, **config_overrides)
        )
        mode = (executor or settings.get_str("ORCHESTRATOR_EXECUTOR", "thread")).strip().lower()
        if plane is not None:
            self.plane = plane
        elif mode == "dramatiq":
            # Durable mode: state in Redis, jobs run on Dramatiq workers (`orchestrator worker`).
            from orchestrator.queue.plane import build_queue_plane

            self.plane = build_queue_plane(on_event=on_event, operator=operator, sandbox=sandbox)
        elif mode == "thread":
            self.plane = LocalControlPlane(on_event=on_event, operator=operator, sandbox=sandbox)
        else:
            raise ValueError(f"unknown executor {mode!r} (thread | dramatiq)")
        self._seeded = False
        if sandbox and plane is None:
            try:
                self.plane.start_stream_server()  # live skill events from the sandbox → job events
            except OSError as exc:
                logger.warning("stream socket unavailable: %s", exc)
        from orchestrator.utils import llm_usage

        llm_usage.enable()  # cost/token telemetry for every LLM call (LiteLLM callbacks)

    # ── catalog ─────────────────────────────────────────────────────────

    def catalog(self) -> PlatformCatalog:
        """The platform catalog, seeded (roles, skills, MCP) on first use."""
        cat = PlatformCatalog.shared()
        if not self._seeded:
            cat.ensure_seeded()
            self._seeded = True
        else:
            try:
                cat.refresh_stale_skills()
            except Exception as exc:  # noqa: BLE001
                logger.warning("skill refresh failed: %s", exc)
        return cat

    def list_roles(self) -> list[dict[str, Any]]:
        self.catalog()
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "goal": r.goal,
                "skills": list(r.skills),
                "reports_to": r.reports_to,
            }
            for r in RoleRegistry.shared().list_roles()
        ]

    def list_skills(self) -> list[dict[str, Any]]:
        self.catalog()
        return [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "provider": s.provider,
                "roles": list(s.role_ids or ()),
            }
            for s in SkillRegistry.shared().list_skills()
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

    def suggest_roles(self, prompt: str) -> list[str]:
        """Roles that fit a brief (LLM router when configured, else the manager only)."""
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt is required")
        self.catalog()
        return RoleRouter.shared().resolve(text, project=False)

    def suggest_skills(self, prompt: str) -> dict[str, Any]:
        """Skills bound to the roles that fit ``prompt``."""
        roles = self.suggest_roles(prompt)
        reg, skills = RoleRegistry.shared(), SkillRegistry.shared()
        ids: list[str] = []
        for rid in roles:
            role = reg.get(rid)
            for sid in (role.skills if role else ()) or ():
                if sid not in ids:
                    ids.append(sid)
        details = [
            {"id": doc.id, "description": doc.description}
            for sid in ids
            if (doc := skills.get(sid)) is not None
        ]
        return {"roles": roles, "skills": ids, "details": details, "prompt": prompt.strip()}

    # ── plan ────────────────────────────────────────────────────────────

    def plan(
        self,
        prompt: str,
        *,
        mode: str = "project",
        project_id: str = "",
        role_ids: list[str] | None = None,
        in_scope: list[str] | None = None,
        exclusions: list[str] | None = None,
    ) -> PlanResult:
        """Draft a plan (``mode`` = ``project`` objectives, or ``job`` single-job plan). No execution."""
        text = (prompt or "").strip()
        if not text:
            return PlanResult(ok=False, error="prompt is required")
        if not llm_configured():
            return PlanResult(ok=False, error="LLM not configured (set an API key; see .env.example)")
        try:
            from orchestrator.planning.draft import draft_llm_plan, read_prior_plan, validate_project_objectives

            self.catalog()
            ws = self.plane.workspace(project_id) if project_id else None
            draft = draft_llm_plan(
                description=text,
                mode=mode,
                role_ids=role_ids,
                in_scope=in_scope or [],
                exclusions=exclusions or [],
                prior_plan=read_prior_plan(ws) if ws else "",
            )
            if draft.mode == "project":
                validate_project_objectives(draft.objectives_payload)
            if ws is not None:
                plans = ws / "plans"
                plans.mkdir(parents=True, exist_ok=True)
                (plans / "latest.md").write_text(draft.plan_text + "\n", encoding="utf-8")
            return PlanResult(
                ok=True,
                plan=draft.plan_text,
                role_ids=draft.role_ids,
                objectives=draft.objectives_payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("plan failed")
            return PlanResult(ok=False, error=str(exc))

    # ── run ─────────────────────────────────────────────────────────────

    def _project(
        self, project_id: str | None, *, title: str = "", roe: Roe | None = None
    ) -> str:
        pid = (project_id or uuid.uuid4().hex[:12]).strip()
        self.plane.project(pid, title=title, roe=roe)
        return pid

    def run(
        self,
        prompt: str,
        *,
        role: str = "",
        skill_names: list[str] | None = None,
        project_id: str | None = None,
        roe: Roe | None = None,
        timeout: float | None = None,
        remove_sandbox: bool | None = None,
    ) -> OrchestratorResult:
        """Run ONE role (one CrewAI agent with its bound skills/tools) on ``prompt``."""
        brief = (prompt or "").strip()
        if not brief:
            return OrchestratorResult(ok=False, error="prompt is required")
        self.catalog()
        rid = (role or "").strip()
        if not rid:
            routed = RoleRouter.shared().resolve(brief, project=False)
            rid = routed[0] if routed else ""
        if not rid:
            return OrchestratorResult(ok=False, error="no role matches this brief; pass role=")
        pid = self._project(project_id, title=brief[:80], roe=roe)
        job = self.plane.submit(
            project_id=pid, title=brief[:80], description=brief,
            role_ids=[rid], skill_ids=list(skill_names or []),
            extras={"dispatch_only": False},   # a single-role run executes itself (no supervisor/worker split)
        )
        job = self.plane.join(job, timeout=timeout)
        result = OrchestratorResult.from_job(job)
        self._cleanup(pid, remove_sandbox)
        return result

    def run_project(
        self,
        brief: str,
        *,
        role_ids: list[str] | None = None,
        project_id: str | None = None,
        roe: Roe | None = None,
        timeout: float | None = None,
        remove_sandbox: bool | None = None,
    ) -> OrchestratorResult:
        """Run the whole crew: manager → specialists (A2A, spawn) → analyzer, as one CrewAI project."""
        text = (brief or "").strip()
        if not text:
            return OrchestratorResult(ok=False, error="brief is required")
        self.catalog()
        manager = RoleRegistry.shared().manager()
        if manager is None:
            return OrchestratorResult(ok=False, error="no manager role in the catalog")
        pid = self._project(project_id, title=text[:80], roe=roe)
        job = self.plane.submit(
            project_id=pid, title=text[:80], description=text,
            role_ids=[manager.id, *(role_ids or [])],
            extras={"crew_mode": "project", "crew_role_ids": list(role_ids or [])},
        )
        job = self.plane.join(job, timeout=timeout)
        self.plane.wait_all(pid, timeout=timeout)
        result = OrchestratorResult.from_job(self.plane.find(job.id, events=True) or job)
        self._cleanup(pid, remove_sandbox)
        return result

    def start_project(
        self,
        brief: str = "",
        *,
        objectives: list[dict[str, Any]] | None = None,
        project_id: str | None = None,
        title: str = "",
        roe: Roe | None = None,
        role_ids: list[str] | None = None,
        kind: str = "timed",
    ) -> str:
        """Plan (unless ``objectives`` is given), store the objectives, and release the ready ones.

        ``kind="durable"`` makes a continuous project: nothing starts until :meth:`accept_plan`, and
        :meth:`create_schedule` re-runs work over time. Returns the project id immediately; the scheduler keeps going as jobs finish — release dependents,
        replan a failed objective, ask the operator when replans run out. Use :meth:`wait_project` to block.
        """
        text = (brief or "").strip()
        if objectives is None:
            if not text:
                raise ValueError("brief or objectives required")
            drafted = self.plan(text, mode="project", role_ids=role_ids)
            if not drafted.ok:
                raise RuntimeError(drafted.error)
            objectives = drafted.objectives
            plan_text = drafted.plan
        else:
            plan_text = ""
        self.catalog()
        if objectives:
            from orchestrator.crew.roles.registry import RoleRegistry

            for item in objectives:
                if not str((item or {}).get("title") or "").strip():
                    raise ValueError("every objective needs a title")
                rid = str(item.get("role_id") or "").strip()
                if rid and RoleRegistry.shared().get(rid) is None:
                    raise ValueError(f"objective {item.get('title')!r}: unknown role {rid!r}")
        pid = (project_id or uuid.uuid4().hex[:12]).strip()
        if kind not in {"timed", "durable"}:
            raise ValueError("kind must be 'timed' or 'durable'")
        self.plane.store.set_project(pid, title=title or text[:80], roe=roe, summary=text, kind=kind,
                                     plan_accepted=kind != "durable", status="active")
        if plan_text:
            plans = self.plane.workspace(pid) / "plans"
            plans.mkdir(parents=True, exist_ok=True)
            (plans / "latest.md").write_text(plan_text + "\n", encoding="utf-8")
        self._bootstrap_memory(pid, title or text[:80], text, roe)
        sched = self.plane.scheduler
        sched.sync_plan(pid, objectives, replace=True)
        sched.enqueue_ready(pid)
        return pid

    def _bootstrap_memory(self, project_id: str, title: str, summary: str, roe: Roe | None) -> None:
        """New project: recall platform-wide lessons for this kind of task into ``memory_bootstrap.md``
        (the manager's memory index shows it). Cognee/memory trouble never blocks a project."""
        try:
            from orchestrator.crew.memory import MemoryService

            recall = MemoryService.recall_for_new_project(
                title=title, summary=summary, scope_hints=(roe.in_scope[:24] if roe else [])
            )
            if recall:
                MemoryService.write_project_bootstrap(project_id, recall)
        except Exception as exc:  # noqa: BLE001
            logger.warning("new-project memory recall failed: %s", exc)

    def accept_plan(self, project_id: str) -> list[str]:
        """Accept a durable project's plan: objectives are released from now on. Returns the started job ids."""
        self.plane.store.set_project(project_id, plan_accepted=True)
        return [j.id for j in self.plane.scheduler.enqueue_ready(project_id)]

    def create_schedule(self, project_id: str, **kwargs: Any):
        """Schedule recurring work: ``role_id`` (+``brief``) or ``objective_seq``; ``kind`` once|interval|cron
        with ``interval_seconds`` / ``cron`` (5 fields). See ``host.schedules``."""
        return self.plane.schedules.create(project_id, **kwargs)

    def list_schedules(self, project_id: str = "") -> list[dict[str, Any]]:
        return [dict(s.__dict__) for s in self.plane.schedules.list(project_id)]

    def delete_schedule(self, schedule_id: str) -> bool:
        return self.plane.schedules.delete(schedule_id)

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        return self.plane.schedules.set_enabled(schedule_id, enabled)

    def fire_due_schedules(self) -> dict[str, Any]:
        """Fire due schedules now (the scheduler service does this every tick)."""
        return self.plane.schedules.fire_due()

    def stop_project(self, project_id: str) -> dict[str, Any]:
        """Operator stop: cancel every open job (the governor too), withdraw open objectives, record the digest."""
        self.plane.operator_control.stop(project_id)
        store = self.plane.store
        for o in store.get_objectives(project_id):
            if o.status in {"pending", "in_progress", "blocked"}:
                store.patch_objective(project_id, o.seq, status="cancelled", note="Stopped by operator")
        self.plane.scheduler.record_digest(project_id, "cancelled")
        return self.project_status(project_id)

    def instruct_project(self, project_id: str, message: str, *, job_id: str | None = None) -> dict[str, Any]:
        """Steer live agents (``job_id`` = one agent and its children). Raises if nothing is running."""
        return self.plane.operator_control.instruct(project_id, message, job_id=job_id)

    def replan_project(self, project_id: str, message: str) -> dict[str, Any]:
        """Revise the plan: resumes the manager with a replan directive (``REPLAN_MODE=plan``: re-draft objectives)."""
        return self.plane.operator_control.replan(project_id, message)

    def chat(self, project_id: str, message: str, *, mode: str = "chat", job_id: str | None = None,
             prompt_id: str | None = None) -> dict[str, Any]:
        """Operator console: routes a message to answer / instruct / replan / stop (or asks one agent via ``job_id``)."""
        from orchestrator.host.console import OperatorConsole

        return OperatorConsole(self.plane).chat(project_id, message, mode=mode, job_id=job_id, prompt_id=prompt_id)

    def restart_project(self, project_id: str) -> None:
        self.plane.operator_control.restart(project_id)

    def reprompt_manager(self, project_id: str, message: str) -> dict[str, Any]:
        return self.plane.operator_control.reprompt_manager(project_id, message)

    def project_status(self, project_id: str) -> dict[str, Any]:
        """``{"status", "objectives": [...], "jobs": [...]}`` — status: empty|running|blocked|completed."""
        sched = self.plane.scheduler
        return {
            "status": sched.project_status(project_id),
            "objectives": [
                {"seq": o.seq, "title": o.title, "role_id": o.role_id, "status": o.status,
                 "depends_on": list(o.depends_on), "note": o.note}
                for o in self.plane.objectives(project_id)
            ],
            "jobs": [
                {"id": j.id, "title": j.title, "status": j.status, "role_ids": j.role_ids,
                 "objective_seq": j.objective_seq, "parent_id": j.parent_id}
                for j in self.plane.jobs(project_id)
            ],
            "prompts": [p for p in self.plane.prompts(project_id) if not p.get("answer")],
        }

    def wait_project(self, project_id: str, timeout: float | None = None, poll: float = 0.3) -> dict[str, Any]:
        """Block until the project settles: ``completed`` / ``completed_with_errors`` / ``cancelled`` (``idle`` for a
        durable one), ``blocked`` (needs the operator), ``paused`` — or ``timeout``."""
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status = self.plane.scheduler.project_status(project_id)
            if status in {"completed", "completed_with_errors", "blocked", "empty", "paused", "idle", "cancelled"}:
                return self.project_status(project_id)
            active = [j for j in self.plane.jobs(project_id) if j.status in {"pending", "running"}]
            if not active:  # only idle agents left: resume those that can be (standbys and the governor wait)
                for j in self.plane.jobs(project_id):
                    if j.status == "idle" and j.idle_note != "dependencies" and j.profile != "governance":
                        self.plane.wake(j.id)
            if deadline is not None and time.monotonic() > deadline:
                return self.project_status(project_id)
            time.sleep(poll)

    def run_objectives(
        self,
        objectives: list[dict[str, Any]],
        *,
        project_id: str | None = None,
        roe: Roe | None = None,
        timeout: float | None = None,
        remove_sandbox: bool | None = None,
    ) -> list[OrchestratorResult]:
        """Run a drafted objective list to completion with the scheduler (dependencies, parallel release,
        replan on failure). Returns one result per objective (its newest job), in objective order."""
        pid = self.start_project(objectives=objectives, project_id=project_id, roe=roe)
        self.wait_project(pid, timeout=timeout)
        out: list[OrchestratorResult] = []
        for obj in self.plane.objectives(pid):
            jobs = [j for j in self.plane.jobs(pid) if j.objective_seq == obj.seq and not j.parent_id]
            if jobs:
                out.append(OrchestratorResult.from_job(self.plane.find(jobs[-1].id, events=True) or jobs[-1]))
        self._cleanup(pid, remove_sandbox)
        return out

    def _cleanup(self, project_id: str, remove: bool | None) -> None:
        if not (self.config.sandbox_remove_on_exit if remove is None else remove):
            return
        if any(j.status not in TERMINAL and j.profile != "governance" for j in self.plane.store.list_jobs(project_id)):
            logger.warning("not removing the sandbox of %s: jobs are still running (the wait timed out)", project_id)
            return
        self.remove_sandbox(project_id)

    def remove_sandbox(self, project_id: str) -> dict[str, Any]:
        """Remove a project's sandbox — on a worker (it holds docker.sock) in durable mode."""
        if type(self.plane.executor).__name__ == "DramatiqExecutor":
            from orchestrator.queue.tasks import cleanup_sandbox

            cleanup_sandbox.send(project_id)
            return {"action": "queued", "project_id": project_id}
        from orchestrator.host.sandbox import ProjectSandbox

        try:
            return ProjectSandbox.remove(project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sandbox cleanup failed: %s", exc)
            return {"action": "error", "errors": [str(exc)]}

    # ── operator control ────────────────────────────────────────────────

    def steer(self, job_id: str, text: str) -> bool:
        """Send operator guidance to a live job (wakes it if idle)."""
        return self.plane.steer(job_id, text)

    def pause(self, job_id: str) -> bool:
        return self.plane.pause(job_id)

    def resume(self, job_id: str) -> bool:
        return self.plane.resume(job_id)

    def kill(self, job_id: str) -> bool:
        return self.plane.kill(job_id)

    def pause_project(self, project_id: str) -> int:
        return self.plane.pause_project(project_id)

    def resume_project(self, project_id: str, *, steer: str = "") -> int:
        return self.plane.operator_control.resume(project_id, steer=steer)

    def reply_to_prompt(self, project_id: str, prompt_id: str, answer: str) -> bool:
        return self.plane.reply_to_prompt(project_id, prompt_id, answer)

    # ── memory / dreaming ───────────────────────────────────────────────

    def recall(self, query: str, *, project_id: str | None = None, role_id: str = "") -> str:
        from orchestrator.memory import store

        return store.recall_text(project_id, query, role_id=role_id)

    def llm_usage_summary(self, *, since_hours: float = 24.0, project_id: str = "") -> list[dict[str, Any]]:
        """LLM cost/tokens/calls grouped by purpose (``planner_project``, ``agent_loop``, ``dream_*`` …)."""
        from orchestrator.utils import llm_usage

        return llm_usage.summary(since_hours=since_hours, project_id=project_id)

    def memory_health(self) -> dict[str, Any]:
        """Cognee sidecar status (``enabled`` False when no server is configured)."""
        from orchestrator.crew.memory import MemoryService

        return MemoryService.health()

    def dream(self, *, limit: int = 20) -> dict[str, Any]:
        """One dream cycle: improve due sessions, curate gists, synthesize playbooks, stage proposals.

        The library starts no threads — call this from your own scheduler.
        """
        from orchestrator.crew.dreaming import DreamingService

        svc = DreamingService.shared()
        svc.run_source = self.plane.run_log
        return svc.run_cycle(limit=limit)


def run(prompt: str, **kwargs: Any) -> OrchestratorResult:
    """Module-level convenience wrapper around :class:`Orchestrator`."""
    return Orchestrator().run(prompt, **kwargs)
