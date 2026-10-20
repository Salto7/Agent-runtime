"""Operator actions on a live project.

``instruct`` steers live agents, ``replan`` revises the plan (by default by resuming the *manager* agent with a
replan directive, like the platform's CrewAI mode; ``REPLAN_MODE=plan`` re-drafts the objectives with the planner instead),
``stop`` / ``restart`` / ``resume`` manage the whole project.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orchestrator import settings
from orchestrator.host.state import TERMINAL, JobState

if TYPE_CHECKING:
    from orchestrator.host.control import LocalControlPlane

logger = logging.getLogger(__name__)

REPLAN_NOTE = "OPERATOR REPLAN — revise the engagement plan and continue under scope rules:\n"
INSTRUCT_NOTE = "OPERATOR INSTRUCTION — revise your approach and continue under scope rules:\n"


class OperatorControl:
    def __init__(self, plane: "LocalControlPlane") -> None:
        self.plane = plane
        self.store = plane.store

    # ── guards ────────────────────────────────────────────────────────────────

    def require_project(self, project_id: str) -> None:
        """Operator actions must not conjure a project: it needs jobs, objectives, a schedule or a brief."""
        meta = self.store.get_project(project_id)
        if (self.store.list_jobs(project_id) or self.store.get_objectives(project_id) or meta.summary or meta.title
                or meta.roe is not None or meta.status != "active" or self.store.list_schedules(project_id)):
            return
        raise ValueError(f"Unknown project {project_id!r}")

    def _allow_operator(self, project_id: str) -> None:
        """Raise if the project cannot take instruct / re-run actions (replan and stop are always allowed)."""
        status = self.store.get_project(project_id).status
        if status == "stopped":
            raise RuntimeError("Project is cancelled — cannot run operator actions")
        if status == "paused":
            raise RuntimeError("Project is paused — resume before running operator actions")

    # ── instruct ──────────────────────────────────────────────────────────────

    def instruct(self, project_id: str, message: str, *, job_id: str | None = None) -> dict[str, Any]:
        """Inject an operator instruction into live agents.

        Targets, in order: the explicit ``job_id`` (and its live children); jobs of in-progress objectives;
        all running (else live) root jobs and their live children. Raises ``RuntimeError`` when nothing is live —
        new work belongs in ``replan``, not in re-running the last finished job.
        """
        self.require_project(project_id)
        text = (message or "").strip()
        if not text:
            raise ValueError("Message is required")
        self._allow_operator(project_id)
        jobs = [j for j in self.store.list_jobs(project_id) if j.profile != "governance"]
        live = lambda j: j.status not in TERMINAL  # noqa: E731
        targets: list[JobState]
        mode = "instruct"
        if job_id:
            sel = next((j for j in jobs if j.id == str(job_id).strip() and live(j)), None)
            if sel is None:
                raise RuntimeError("Selected agent is not running (or not on this project)")
            targets = [sel, *[c for c in jobs if c.parent_id == sel.id and live(c)]]
            mode = "instruct_selected"
        else:
            open_objs = {o.seq for o in self.store.get_objectives(project_id) if o.status == "in_progress"}
            targets = [j for j in jobs if j.objective_seq in open_objs and j.status in {"running", "pending", "paused", "idle"}]
            if not targets:
                roots = [j for j in jobs if not j.parent_id]
                targets = [j for j in roots if j.status == "running"] or [
                    j for j in roots if j.status in {"running", "pending", "paused", "idle"}]
                seen = {j.id for j in targets}
                for root in list(targets):
                    for c in jobs:
                        if c.parent_id == root.id and live(c) and c.id not in seen:
                            targets.append(c)
                            seen.add(c.id)
        if not targets:
            raise RuntimeError("No running agents to instruct — use replan for new work (e.g. rescan a port) "
                               "instead of re-running the last finished job")
        steered: list[str] = []
        for job in targets:
            self.store.add_guidance(job.id, INSTRUCT_NOTE + text)
            steered.append(job.id)
            if job.status == "idle" and job.idle_note != "dependencies":
                self.plane.wake(job.id)   # an operator instruction wakes a parked agent (not a dependency standby)
        primary = targets[0]
        self.plane._remember_feedback(project_id, text, source="operator instruction")
        self.plane.store.add_event(primary.id, {"job_id": primary.id, "project_id": project_id,
                                                "message_type": "log", "content": text,
                                                "metadata": {"event": "project_instruction", "role": "user"}, "at": ""})
        return {"mode": mode, "job_ids": steered, "primary_job_id": primary.id}

    # ── manager job / replan ──────────────────────────────────────────────────

    def manager_job(self, project_id: str) -> JobState | None:
        """The engagement manager's job (not the resident governor). Prefer one still in play; a cancelled one is
        reused only when it carries the manager objective."""
        from orchestrator.crew.roles.registry import RoleRegistry

        mgr = RoleRegistry.shared().manager()
        if mgr is None:
            return None
        cands = sorted((j for j in self.store.list_jobs(project_id)
                        if j.profile != "governance" and mgr.id in j.role_ids), key=lambda j: j.created_at, reverse=True)
        for j in cands:
            if j.status != "cancelled":
                return j
        return next((j for j in cands if j.objective_seq), None)

    def ensure_manager_job(self, project_id: str, *, resume: bool = False, replan: bool = False, steer: str = "") -> JobState:
        """Find or create the manager job and queue it (with the steer/replan text as guidance)."""
        from orchestrator.crew.roles.registry import RoleRegistry

        mgr = RoleRegistry.shared().manager()
        if mgr is None:
            raise RuntimeError("No manager role is loaded; check the role catalog and skills/")
        meta = self.store.get_project(project_id)
        job = self.manager_job(project_id)
        if job is None:
            job = self.plane.submit(project_id=project_id, title=mgr.label or "Project Manager",
                                    description=(steer or meta.summary or meta.title or "")[:8000],
                                    role_ids=[mgr.id], dispatch=False)
        elif job.status != "running":
            if steer:
                self.store.patch_job(job.id, description=(f"{job.description}\n\nOPERATOR:\n{steer}".strip())[:8000])
            self.plane.scheduler.reopen_for_job(job)
            self.store.patch_job(job.id, status="pending", error="", control="", idle_note="")
        if replan and steer:
            self.store.add_guidance(job.id, REPLAN_NOTE + steer)
        if job.status != "running":
            self.plane.executor.dispatch(job.id, bool(resume and job.id == (self.manager_job(project_id) or job).id
                                                      and self.store.latest_checkpoint(job.id)), "")
        return self.store.get_job(job.id) or job

    def replan(self, project_id: str, message: str) -> dict[str, Any]:
        """Revise the plan. ``REPLAN_MODE=manager`` (default): resume the manager with a replan directive;
        ``plan``: re-draft the objectives with the planner and re-sync them (needs a project brief)."""
        self.require_project(project_id)
        text = (message or "").strip()
        if not text:
            raise ValueError("Replan message is required")
        status = self.store.get_project(project_id).status
        if status == "stopped":
            raise RuntimeError("Project is cancelled — cannot replan")
        if status == "paused":
            self.store.set_project(project_id, status="active")
        if settings.get_str("REPLAN_MODE", "manager").lower() == "plan":
            self.plane.scheduler.replan(project_id, text)
            return {"mode": "replan", "job_ids": [], "primary_job_id": "",
                    "objectives": len(self.store.get_objectives(project_id)), "plan_preview": text[:240]}
        job = self.ensure_manager_job(project_id, resume=True, replan=True, steer=text)
        return {"mode": "crew_replan", "job_ids": [job.id], "primary_job_id": job.id, "objectives": 0,
                "plan_preview": text[:240]}

    def reprompt_manager(self, project_id: str, message: str, *, kind: str = "steer") -> dict[str, Any]:
        """Inject operator text into the manager job and (re)queue it."""
        self.require_project(project_id)
        text = (message or "").strip()
        if not text:
            raise ValueError("Reprompt message is required")
        self._allow_operator(project_id)
        job = self.ensure_manager_job(project_id, resume=True, steer=text)
        self.store.add_guidance(job.id, text)
        return {"mode": "crew_reprompt", "job_ids": [job.id], "primary_job_id": job.id}

    # ── whole-project actions ─────────────────────────────────────────────────

    def stop(self, project_id: str) -> int:
        """Cancel every in-flight job (the governor too) and mark the project stopped. The data is kept."""
        self.require_project(project_id)
        self.store.set_project(project_id, status="stopped")
        n = 0
        for j in self.store.list_jobs(project_id):
            if j.status in TERMINAL:
                continue
            if j.profile == "governance":
                if self.plane.cancel(j.id):
                    self.store.patch_job(j.id, error="Stopped with project")
                    n += 1
            elif self.plane.kill(j.id, cascade=False):
                n += 1
        return n

    def restart(self, project_id: str) -> None:
        """Stop in-flight attempts and dispatch the plan again (open objectives are released by the scheduler)."""
        self.require_project(project_id)
        manager = self.manager_job(project_id)
        for j in self.store.list_jobs(project_id):
            if j.status in TERMINAL or j.profile == "governance" or (manager and j.id == manager.id):
                continue
            self.plane.cancel(j.id)
            self.store.patch_job(j.id, error="Restarted by operator")
        self.store.set_project(project_id, status="active")
        self.ensure_manager_job(project_id, resume=False)
        self.plane.governance.ensure(project_id)
        self.plane.scheduler.enqueue_ready(project_id)

    def resume(self, project_id: str, *, steer: str = "") -> int:
        """Resume a paused project: its paused jobs continue from their checkpoints; optional guidance to the manager."""
        self.require_project(project_id)
        if self.store.get_project(project_id).status == "stopped":
            raise RuntimeError("Project is cancelled — cannot resume (use restart_project)")
        n = self.plane.resume_project(project_id)
        if (steer or "").strip():
            self.reprompt_manager(project_id, steer)
        self.plane.scheduler.enqueue_ready(project_id)
        return n
