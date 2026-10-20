"""Objective scheduling — the library port of Peon's ``ObjectiveScheduler`` / ``ObjectivePlanSync``.

A project plan is a list of objectives with ``depends_on`` edges (sequence numbers). The scheduler:

* **releases** every objective whose dependencies are completed (parallel by default, no serial stall),
  and creates an ``idle`` *standby* agent for the ones still waiting, so the whole crew exists from the
  moment the plan is set; a standby agent is woken — with the finished dependencies' results delivered as
  A2A handoffs — when its last dependency completes;
* **settles** an objective when all of its jobs are terminal (completed / blocked on a failure);
* on a failed top-level objective job, **replans** (LLM-drafted, ``replace=True`` plan sync) up to
  ``AGENT_MAX_FAILURE_REPLANS`` times, then **raises an operator prompt** and leaves the objective blocked;
* resumes blocked objectives when the operator replies.

All state is in the ``StateStore``; the scheduler holds none. Cross-process safety comes from the store's
named lock (``store.lock``), so concurrent workers finishing jobs at the same time cannot double-release.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from orchestrator.host.state import TERMINAL, JobState, Objective

if TYPE_CHECKING:
    from orchestrator.host.control import LocalControlPlane

logger = logging.getLogger(__name__)

DEP_NOTE = "dependencies"  # ``idle_note`` of a standby agent waiting on objective dependencies
OPEN = {"pending", "in_progress"}
DONE = {"completed", "cancelled"}


class ObjectiveScheduler:
    def __init__(self, plane: "LocalControlPlane") -> None:
        self.plane = plane
        self.store = plane.store

    # ── plan sync (Peon: ObjectivePlanSync) ────────────────────────────────────

    def sync_plan(self, project_id: str, payload: list[dict[str, Any]], *, replace: bool = True) -> list[Objective]:
        """Create/update objectives from planner JSON; ``depends_on`` are 1-based list indices (or seqs).

        With ``replace`` a replan re-opens matched terminal objectives (detaching their prior jobs so
        settling cannot be confused by the earlier attempt) and cancels open ones the new plan dropped.
        """
        with self.store.lock(f"sched:{project_id}"):
            planned = [x for x in (payload or []) if isinstance(x, dict)]
            existing = self.store.get_objectives(project_id)
            by_title = {o.title.strip().lower(): o for o in existing}
            used = [int(o.seq) for o in existing]
            nxt = max(used, default=0) + 1
            result: list[Objective] = []
            keep: set[int] = set()
            for i, item in enumerate(planned, start=1):
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                skills = [str(s).strip() for s in (item.get("skill_ids") or []) if str(s).strip()]
                commands = [str(c).strip() for c in (item.get("commands") or []) if str(c).strip()]
                fields = dict(
                    description=str(item.get("description") or "").strip(),
                    acceptance=str(item.get("acceptance_criteria") or item.get("acceptance") or "").strip(),
                    phase=str(item.get("phase") or "").strip().lower(),
                    role_id=str(item.get("role_id") or "").strip(),
                    skill_ids=skills,
                    commands=commands,
                )
                obj = by_title.get(title.lower())
                if obj is None:
                    obj = Objective(seq=nxt, title=title, **fields)
                    nxt += 1
                else:
                    for k, v in fields.items():
                        if v:
                            setattr(obj, k, v)
                    if replace and obj.status in {"completed", "cancelled", "blocked"}:
                        for j in self.store.list_jobs(project_id):
                            if j.objective_seq == obj.seq:
                                self.store.patch_job(j.id, objective_seq=0)
                        obj.status, obj.note = "pending", ""
                keep.add(obj.seq)
                result.append(obj)
            # depends_on: planner indices refer to positions in THIS plan (1-based)
            by_pos = {pos: o for pos, o in zip((i for i, x in enumerate(planned, start=1)
                                                if str(x.get("title") or "").strip()), result)}
            for obj, item in zip(result, [x for x in planned if str(x.get("title") or "").strip()]):
                deps: list[int] = []
                for ref in item.get("depends_on") or []:
                    try:
                        idx = int(ref)
                    except (TypeError, ValueError):
                        continue
                    target = by_pos.get(idx)   # planner indices count every item, titled or not
                    if target is not None and target.seq != obj.seq:
                        deps.append(target.seq)
                obj.depends_on = sorted(set(deps))
            self._break_cycles(result)
            merged = {o.seq: o for o in result}
            if replace:
                for old in existing:
                    if old.seq in merged:
                        continue
                    if old.status != "completed":
                        old.status, old.note = "cancelled", "Superseded by replanned project plan"
                    merged[old.seq] = old
            else:
                for old in existing:
                    merged.setdefault(old.seq, old)
            self.store.set_objectives(project_id, sorted(merged.values(), key=lambda o: o.seq))
            self.sweep_orphan_standby(project_id)
            return [o for o in self.store.get_objectives(project_id) if o.status != "cancelled"]

    @staticmethod
    def _break_cycles(objs: list[Objective]) -> None:
        """Drop the dependency edges that close a cycle — a cyclic plan would otherwise wait on itself forever."""
        by_seq = {o.seq: o for o in objs}
        state: dict[int, int] = {}   # 1 = on the DFS stack, 2 = done

        def visit(seq: int) -> None:
            state[seq] = 1
            obj = by_seq[seq]
            for dep in list(obj.depends_on):
                if dep not in by_seq:
                    continue
                if state.get(dep) == 1:
                    obj.depends_on.remove(dep)
                    logger.warning("plan: dropped dependency OBJ-%s -> OBJ-%s (cycle)", seq, dep)
                elif dep not in state:
                    visit(dep)
            state[seq] = 2

        for o in objs:
            if o.seq not in state:
                visit(o.seq)

    def sweep_orphan_standby(self, project_id: str) -> int:
        """Cancel idle agents whose objective vanished, finished or was cancelled (Peon: ``sweep_orphan_standby``)."""
        by_seq = {o.seq: o for o in self.store.get_objectives(project_id)}
        n = 0
        for j in self.store.list_jobs(project_id):
            if j.status != "idle" or j.parent_id or j.profile == "governance":
                continue
            obj = by_seq.get(j.objective_seq)
            if obj is None or obj.status in {"cancelled", "completed"}:
                if self.store.claim_job(j.id, {"idle"}, "cancelled"):
                    self.store.patch_job(j.id, error="Superseded: objective no longer open", idle_note="")
                    n += 1
        return n

    # ── queries ────────────────────────────────────────────────────────────────

    def _objective(self, project_id: str, seq: int) -> Objective | None:
        return next((o for o in self.store.get_objectives(project_id) if o.seq == seq), None)

    def dependencies_met(self, project_id: str, obj: Objective) -> bool:
        if not obj.depends_on:
            return True
        by_seq = {o.seq: o for o in self.store.get_objectives(project_id)}
        # a cancelled (withdrawn) dependency can never complete: it must not hold its dependents forever
        return all(by_seq.get(d) is not None and by_seq[d].status in {"completed", "cancelled"} for d in obj.depends_on)

    def jobs_of(self, project_id: str, seq: int) -> list[JobState]:
        return [j for j in self.store.list_jobs(project_id) if j.objective_seq == seq]

    def active_jobs(self, project_id: str, seq: int) -> list[JobState]:
        return [j for j in self.jobs_of(project_id, seq) if j.status not in TERMINAL]

    def ready_objectives(self, project_id: str) -> list[Objective]:
        """Objectives that may get a job now: pending with deps met, plus orphaned in-progress ones."""
        out: list[Objective] = []
        for obj in self.store.get_objectives(project_id):
            if obj.status == "in_progress":
                jobs = self.jobs_of(project_id, obj.seq)
                # not orphaned while a job is live, or while a finished job is still waiting to be
                # settled (its hook runs just after the job turns terminal — releasing here would run it twice)
                if any(j.status not in TERMINAL or j.status in {"completed", "failed"} for j in jobs):
                    continue
            elif obj.status != "pending" or obj.note == "scheduled":
                continue  # (a "scheduled" objective waits for its schedule to fire)
            if not self.dependencies_met(project_id, obj):
                continue
            if not obj.role_id.strip():
                self.mark(project_id, obj.seq, "blocked", reason="No role_id — assign a role or replan")
                continue
            out.append(obj)
        return out

    def project_status(self, project_id: str) -> str:
        """Derived project state (nothing stored) — Peon's ``reconcile_project_status`` as a pure function.

        ``empty`` | ``running`` | ``paused`` | ``blocked`` | ``completed`` | ``cancelled`` | ``idle`` (durable,
        between cycles). ``blocked`` is Peon's *finished with errors*: a blocked objective or a failed job and
        nothing left that could move. Idle agents are standby, not work in flight: they keep the project open
        only while something can still wake them (a running/queued agent, a ready objective, an open prompt).
        """
        meta = self.store.get_project(project_id)
        if meta.status == "stopped":
            return "cancelled"
        jobs = [j for j in self.store.list_jobs(project_id) if j.profile != "governance"]
        objs = self.store.get_objectives(project_id)
        if not jobs and not objs:
            return "empty"
        if meta.status == "paused" or (any(j.status == "paused" for j in jobs)
                                       and not any(j.status in {"pending", "running"} for j in jobs)):
            return "paused"
        active_now = any(j.status not in TERMINAL and j.status != "idle" for j in jobs)
        if meta.kind == "durable" and not meta.plan_accepted and not active_now:
            return "idle"   # waiting for the operator to accept the plan: nothing runs, nothing to wait on
        has_idle = any(j.status == "idle" for j in jobs)
        has_ready = bool(self.ready_objectives(project_id))
        open_prompts = any(not p.get("answer") for p in self.store.get_prompts(project_id))
        statuses = [o.status for o in objs]
        busy = active_now or (has_idle and (has_ready or open_prompts))
        if busy or has_ready or (any(st in OPEN for st in statuses) and any(st == "in_progress" for st in statuses)):
            return "running"
        live = [o for o in objs if o.status != "cancelled"]
        if meta.kind == "durable":
            scheduled = {sc.objective_seq for sc in self.store.list_schedules(project_id) if sc.enabled}
            if live and all(o.status == "completed" or (o.status == "pending" and o.seq in scheduled) for o in live):
                return "idle"  # between cycles: schedules fire the next run
        if statuses and all(st == "cancelled" for st in statuses):
            return "cancelled"
        if any(st == "blocked" for st in statuses):
            return "blocked"
        if statuses and all(st in {"completed", "cancelled"} for st in statuses):
            # Peon: a failed job anywhere makes the finished project "finished with errors" (even after a replan)
            return "completed_with_errors" if any(j.status == "failed" for j in jobs) else "completed"
        if any(j.status == "failed" for j in jobs):
            return "blocked"
        if jobs and all(j.status == "cancelled" for j in jobs):
            return "cancelled"
        return "completed" if not any(st in OPEN for st in statuses) else "running"

    # ── transitions ────────────────────────────────────────────────────────────

    def mark(self, project_id: str, seq: int, status: str, *, reason: str = "") -> bool:
        """Set an objective's status with Peon's guards (no completing with live jobs; no reopening completed)."""
        obj = self._objective(project_id, seq)
        if obj is None:
            return False
        if status == "completed" and self.active_jobs(project_id, seq):
            return False
        if status == "in_progress" and obj.status == "completed":
            return False
        note = (reason or "").strip()[:2000] if status in {"blocked", "cancelled"} else ""
        return self.store.patch_objective(project_id, seq, status=status, note=note)

    def reopen_for_job(self, job: JobState) -> None:
        """A completed job was resumed (a message reached it): its objective is open again."""
        if not job.objective_seq:
            return
        obj = self._objective(job.project_id, job.objective_seq)
        if obj is not None and obj.status == "completed":
            self.store.patch_objective(job.project_id, obj.seq, status="in_progress", note="")

    def resume_blocked(self, project_id: str, seq: int, *, reason: str = "Operator replied") -> bool:
        obj = self._objective(project_id, seq)
        if obj is None or obj.status != "blocked":
            return False
        return self.store.patch_objective(project_id, seq, status="in_progress", note="")

    def settle(self, project_id: str, seq: int) -> str:
        """Advance an objective once every job tagged to it has finished. Leaves ``blocked`` alone.

        The outcome follows the newest job that actually finished; a cancelled attempt is not an outcome.
        """
        obj = self._objective(project_id, seq)
        if obj is None or obj.status in {"completed", "cancelled", "blocked"}:
            return obj.status if obj else ""
        jobs = self.jobs_of(project_id, seq)
        if any(j.status not in TERMINAL for j in jobs):
            return obj.status
        roots = [j for j in jobs if not j.parent_id] or jobs    # the outcome is the top-level jobs (a failed child is its parent's to handle)
        finished = sorted((j for j in roots if j.status != "cancelled"), key=lambda j: j.created_at)
        if not finished:
            if jobs:  # only cancelled attempts: stay open so another attempt can be queued
                return obj.status
            self.mark(project_id, seq, "completed")
            return "completed"
        latest = finished[-1]
        if latest.status == "failed":
            self.mark(project_id, seq, "blocked", reason=(latest.error or "One or more objective jobs failed"))
            return "blocked"
        self.mark(project_id, seq, "completed")
        return "completed"

    # ── releasing work ─────────────────────────────────────────────────────────

    def build_brief(self, project_id: str, obj: Objective) -> str:
        meta = self.store.get_project(project_id)
        parts = [
            f"Execute project objective OBJ-{obj.seq}: {obj.title}",
            f"Description: {obj.description or '(none)'}",
            f"Acceptance criteria: {obj.acceptance or '(none)'}",
        ]
        if obj.phase:
            parts.insert(1, f"Phase: {obj.phase}")
        parts += ["", f"Project: {meta.title or project_id}"]
        if meta.roe is not None:
            parts += ["Rules of engagement:", meta.roe.summary()]
        parts += [
            "",
            "record_finding is for discoveries about subjects with evidence — not job/objective/agent progress. "
            "Write evidence under workspace/; curated notes under findings/<role-id>.md. Do not write "
            "findings/report.md unless this objective is the analyzer.",
        ]
        if obj.note and obj.status != "blocked":
            parts += ["", f"Operator guidance: {obj.note}"]
        return "\n".join(parts)

    def create_run(self, project_id: str, obj: Objective, *, standby: bool = False, extra: str = "") -> JobState | None:
        """Create the objective's job. ``standby`` makes it ``idle`` (not queued) while dependencies are unmet."""
        if obj.status in DONE or self.active_jobs(project_id, obj.seq):
            return None
        met = self.dependencies_met(project_id, obj)
        if not met and not standby:
            return None
        role = obj.role_id.replace(";", ",").split(",")[0].strip()
        if not role:
            self.mark(project_id, obj.seq, "blocked", reason="No role_id — assign a role or replan")
            return None
        brief = self.build_brief(project_id, obj) + (f"\n\n{extra}" if extra else "")
        job = JobState(
            id=uuid.uuid4().hex[:12],
            project_id=project_id,
            title=f"OBJ-{obj.seq}: {obj.title}"[:255],
            description=brief,
            role_ids=[role],
            skill_ids=list(obj.skill_ids),
            objective_seq=obj.seq,
            status="pending" if met else "idle",
            idle_note="" if met else DEP_NOTE,
        )
        self.store.put_job(job)
        if not met:
            return job  # standby: woken by the dependency handoff
        if obj.status != "in_progress":
            self.store.patch_objective(project_id, obj.seq, status="in_progress", note="")
        self.plane.executor.dispatch(job.id, False, "")
        try:
            from orchestrator.skills.role_memory import upsert_role_skills

            upsert_role_skills(role, list(obj.skill_ids), project_id=project_id, source="objective_run")
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_role_skills failed: %s", exc)
        return job

    _HANDOFF_EXCERPT = 1800

    def handoff_text(self, project_id: str, dep: Objective, job: JobState | None, findings: int) -> str:
        who = ((job.role_ids[0] if job and job.role_ids else "") or dep.role_id or "agent")
        lines = [f'Handoff from OBJ-{dep.seq} "{dep.title}" ({who}) — completed.']
        result = self.plane._result_excerpt(job, self._HANDOFF_EXCERPT) if job else ""
        if result:
            lines.append("Result:\n" + result)
        if job is not None:
            kids = sorted((c for c in self.store.list_jobs(project_id) if c.parent_id == job.id and c.status == "completed"),
                          key=lambda c: c.created_at)[:5]
            for child in kids:
                part = self.plane._result_excerpt(child, self._HANDOFF_EXCERPT // 2)
                if part:
                    lines.append(f"Sub-agent {(child.role_ids[0] if child.role_ids else '') or child.title} — {child.title}:\n{part}")
        if findings:
            lines.append(f"{findings} finding(s) were recorded for that objective — use `list_findings` for details.")
        lines.append("Use `list_workspace_artifacts` / `read_workspace_artifact` for its files.")
        return "\n\n".join(lines)

    def deliver_handoffs(self, project_id: str, obj: Objective, standby: JobState) -> None:
        """Send each completed dependency's result to the objective's standby agent (A2A handoff, deduped)."""
        by_seq = {o.seq: o for o in self.store.get_objectives(project_id)}
        findings = self.store.get_findings(project_id)
        inbox = self.plane.hub.inbox(standby.id)
        for dep_seq in obj.depends_on:
            dep = by_seq.get(dep_seq)
            if dep is None or dep.status != "completed":
                continue
            done_jobs = sorted((j for j in self.jobs_of(project_id, dep_seq) if j.status == "completed" and not j.parent_id),
                               key=lambda j: j.created_at)
            done = done_jobs[-1] if done_jobs else None
            if done is not None and any(m.kind == "handoff" and m.sender_job_id == done.id for m in inbox):
                continue
            count = sum(1 for f in findings if f.get("objective_seq") == dep_seq)
            try:
                self.plane.hub.send(
                    sender_job_id=done.id if done else f"obj-{dep_seq}", recipient_job_id=standby.id, kind="handoff",
                    content=self.handoff_text(project_id, dep, done, count),
                    metadata={"from_objective_seq": dep.seq, "from_job_id": done.id if done else "", "findings": count},
                    backstop=self.plane.interim_backstop,
                )  # no wake: the scheduler wakes it once, when every dependency is met (standbys are exempt)
            except ValueError as exc:
                logger.debug("dependency handoff skipped: %s", exc)

    def enqueue_ready(self, project_id: str) -> list[JobState]:
        """Start every ready objective (or wake its standby agent); give the rest a standby agent."""
        started: list[JobState] = []
        meta = self.store.get_project(project_id)
        if meta.status != "active" or (meta.kind == "durable" and not meta.plan_accepted):
            return started  # paused/stopped, or a durable plan the operator has not accepted yet
        with self.store.lock(f"sched:{project_id}"):
            try:
                self.sweep_orphan_standby(project_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("standby sweep failed: %s", exc)
            ready = self.ready_objectives(project_id)
            try:   # a finished project with nothing to release must not grow a new idle governor
                if ready or any(j.status not in TERMINAL for j in self.store.list_jobs(project_id)):
                    self.plane.governance.ensure(project_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("governance PM failed: %s", exc)
            for obj in ready:
                standby = next((j for j in self.jobs_of(project_id, obj.seq)
                                if j.status == "idle" and j.idle_note == DEP_NOTE and not j.parent_id), None)
                if standby is not None:
                    self.deliver_handoffs(project_id, obj, standby)
                    self.store.patch_job(standby.id, idle_note="")
                    self.store.patch_objective(project_id, obj.seq, status="in_progress", note="")
                    self.plane.wake(standby.id, force=True)
                    started.append(standby)
                    continue
                job = self.create_run(project_id, obj)
                if job is not None:
                    started.append(job)
            ready_seqs = {o.seq for o in ready}
            for obj in self.store.get_objectives(project_id):
                if obj.seq in ready_seqs or obj.status != "pending" or not obj.role_id.strip() or obj.note == "scheduled":
                    continue
                if not self.active_jobs(project_id, obj.seq):
                    self.create_run(project_id, obj, standby=True)
        return started

    # ── reacting to finished jobs ──────────────────────────────────────────────

    def on_job_finished(self, job: JobState) -> None:
        """Called by the plane when a job reaches a terminal state (in the worker that ran it)."""
        if not job.objective_seq:
            manager = self._manager_id()
            if job.status == "failed" and manager and job.role_ids == [manager] and not job.profile:
                self.failure_replan(job)   # a failed replan manager: surface the blocked work to the operator
            return
        pid = job.project_id
        recovery = False
        with self.store.lock(f"sched:{pid}"):
            obj = self._objective(pid, job.objective_seq)
            if obj is None:
                return
            if job.status == "failed" and not job.parent_id and obj.status in OPEN:
                self.mark(pid, obj.seq, "blocked", reason=(job.error or "Job failed"))
                recovery = True
            self.settle(pid, obj.seq)
        if recovery:
            recovery = self.failure_replan(job)
        if not recovery:
            self.enqueue_ready(pid)
        status = self.project_status(pid)
        if self.store.get_project(pid).kind == "durable":
            if status in {"completed", "completed_with_errors", "idle"}:
                self.plane.schedules.rearm_completed(pid)  # a durable cycle ended: schedules drive the next one
        elif status in {"completed", "completed_with_errors"}:
            self.plane.governance.cancel(pid)
            self.record_digest(pid, "finished" if status == "completed" else "finished_with_errors")

    def record_digest(self, project_id: str, status: str) -> None:
        """Project-scope memory digest of the engagement's outcome (Peon: ``record_project_digest``)."""
        try:
            from orchestrator.memory import store as memory

            memory.record_project_digest(
                project_id,
                status,
                objectives=[(o.title, o.status) for o in self.store.get_objectives(project_id)],
                findings=[(f.get("title", ""), f.get("severity", "info"), f.get("kind", ""))
                          for f in self.store.get_findings(project_id)],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_project_digest failed: %s", exc)

    def maybe_reopen_analyzer_after_child(self, job: JobState) -> JobState | None:
        """A spawned child finished: re-run the analyzer so its report covers the fresh artifacts."""
        from orchestrator.crew.roles.registry import RoleRegistry

        reg = RoleRegistry.shared()
        ana, mgr = reg.analyzer(), reg.manager()
        roles = set(job.role_ids)
        if not job.parent_id or (ana and ana.id in roles) or (mgr and mgr.id in roles):
            return None
        return self.reopen_analyzer(job.project_id)

    def reopen_analyzer(self, project_id: str) -> JobState | None:
        """Re-open the analyzer objective (and re-queue its latest job) so the report rewrites."""
        from orchestrator.crew.roles.registry import RoleRegistry

        ana = RoleRegistry.shared().analyzer()
        if ana is None:
            return None
        objs = [o for o in self.store.get_objectives(project_id) if o.role_id == ana.id and o.status != "cancelled"]
        if not objs:
            return None
        obj = objs[-1]
        if obj.status != "pending":
            self.store.patch_objective(project_id, obj.seq, status="pending", note="")
        jobs = sorted(self.jobs_of(project_id, obj.seq), key=lambda j: j.created_at)
        if jobs:
            return self.plane.requeue(jobs[-1].id)
        for j in self.enqueue_ready(project_id):
            if ana.id in j.role_ids:
                return j
        return None

    def failure_replan(self, job: JobState) -> bool:
        """Replan after a failed specialist job, or raise an operator prompt when replans are exhausted."""
        from orchestrator.config import get_config

        pid = job.project_id
        manager = self._manager_id()
        if manager and job.role_ids == [manager]:
            # the manager failing is not replanned by the manager — but a blocked project must not be left without
            # a way forward: ask the operator about the specialist work that is still blocked.
            blocked = [o for o in self.store.get_objectives(pid) if o.status == "blocked"]
            failed = [j for j in sorted(self.store.list_jobs(pid), key=lambda j: j.created_at)
                      if j.status == "failed" and j.objective_seq and manager not in j.role_ids
                      and any(o.seq == j.objective_seq for o in blocked)]
            if failed:
                self._exhausted_prompt(failed[-1], fails=len(failed), max_replans=max(0, int(get_config().agent_max_failure_replans)))
            return False
        max_replans = max(0, int(get_config().agent_max_failure_replans))
        fails = sum(1 for j in self.store.list_jobs(pid) if j.status == "failed" and j.objective_seq)
        if fails > max_replans:
            self._exhausted_prompt(job, fails=fails, max_replans=max_replans)
            return False
        obj = self._objective(pid, job.objective_seq)
        note = (
            f"Specialist job failed for OBJ-{obj.seq if obj else '?'} ({', '.join(job.role_ids) or 'unknown role'}): "
            f"{(job.error or 'failed')[:500]}\nReplan: unblock or replace the failed work; keep completed "
            "objectives; ensure the analyzer can still run when enough evidence exists."
        )
        try:  # REPLAN_MODE: resume the manager with a replan directive (default) or re-draft the plan
            return bool(self.plane.operator_control.replan(pid, note))
        except Exception as exc:  # noqa: BLE001
            logger.warning("failure replan failed for %s: %s", pid, exc)
            self._exhausted_prompt(job, fails=fails, max_replans=max_replans)
            return False

    def replan(self, project_id: str, note: str = "") -> bool:
        """Re-draft the project plan (LLM) from the brief + prior plan + current objectives and re-sync it."""
        from orchestrator.planning.draft import draft_llm_plan, read_prior_plan, validate_project_objectives

        meta = self.store.get_project(project_id)
        brief = (meta.summary or meta.title or "").strip()
        if not brief:
            raise ValueError("project has no brief to replan from")
        ws = self.plane.workspace(project_id)
        roe = meta.roe
        draft = draft_llm_plan(
            description=brief + (f"\n\nREPLAN NOTE:\n{note}" if note else ""),
            mode="project",
            title=meta.title or "adhoc",
            summary=brief,
            in_scope=roe.in_scope if roe else (),
            exclusions=roe.exclusions if roe else (),
            prior_plan=read_prior_plan(ws),
            existing_objectives=[
                f"- OBJ-{o.seq} [{o.status}] {o.title} role={o.role_id or '-'}"
                for o in self.store.get_objectives(project_id)[:40]
            ],
        )
        validate_project_objectives(draft.objectives_payload)
        (ws / "plans").mkdir(parents=True, exist_ok=True)
        (ws / "plans" / "latest.md").write_text(draft.plan_text + "\n", encoding="utf-8")
        self.sync_plan(project_id, draft.objectives_payload, replace=True)
        self.enqueue_ready(project_id)
        return True

    def _manager_id(self) -> str:
        from orchestrator.crew.roles.registry import RoleRegistry

        manager = RoleRegistry.shared().manager()
        return manager.id if manager else ""

    def _exhausted_prompt(self, job: JobState, *, fails: int, max_replans: int) -> None:
        pid = job.project_id
        for p in self.store.get_prompts(pid):
            if p.get("kind") == "failure_exhausted" and p.get("objective_seq") == job.objective_seq and not p.get("answer"):
                return  # one open prompt per objective
        prompt = {
            "id": uuid.uuid4().hex[:12],
            "job_id": job.id,
            "kind": "failure_exhausted",
            "objective_seq": job.objective_seq,
            "question": (
                f"Failure replans exhausted ({fails}/{max_replans}) for OBJ-{job.objective_seq} "
                f"({', '.join(job.role_ids) or 'unknown role'}): {(job.error or 'failed')[:500]}\n"
                "Reply 'approve' (retry), any guidance text (retry with that guidance), or 'deny' (leave blocked)."
            ),
            "options": ["approve", "edit", "deny"],
            "blocks_job": False,
            "answer": "",
        }
        self.plane.raise_prompt(pid, prompt)  # a host hook may answer synchronously

    def resolve_prompt(self, project_id: str, prompt: dict[str, Any], reply: str) -> bool:
        """Approve / guide / deny a *failure-exhausted* prompt. Returns True if this kind was handled here.

        ``deny`` leaves the objective blocked; anything else resumes it — the failed job is re-queued with the
        operator's guidance (``approve`` = plain retry), exactly like Peon's steer + ``queue_job``.
        """
        if prompt.get("kind") != "failure_exhausted":
            return False
        seq = int(prompt.get("objective_seq") or 0)
        text = (reply or "").strip()
        if not seq or text.lower() in {"deny", "no", "stop", ""}:
            return True  # left blocked
        guidance = "" if text.lower() in {"approve", "yes", "ok", "retry"} else f"OPERATOR GUIDANCE: {text}"
        if not self.resume_blocked(project_id, seq):
            return True
        failed = self.store.get_job(prompt.get("job_id", ""))
        if failed is not None and failed.status in TERMINAL and failed.status != "cancelled":
            if guidance:
                self.store.add_guidance(failed.id, guidance)
            self.plane.requeue(failed.id)          # Peon: steer the failed job + queue_job
            return True
        obj = self._objective(project_id, seq)
        if obj is not None:
            self.create_run(project_id, obj, extra=guidance)
        return True
