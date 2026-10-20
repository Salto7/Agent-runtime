"""Durable schedules — the library port of Peon's ``schedules.py``.

A durable project keeps working over time: a schedule (cron / interval / once) fires a job for a role, or re-runs
an objective, whenever it is due. ``fire_due`` is meant to be called from a periodic loop (the scheduler service
does, every tick); it is safe to run from several processes (each schedule is fired under a store lock).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from orchestrator.host.checkpoints import write_checkpoint
from orchestrator.host.state import TERMINAL, JobState, Schedule
from orchestrator.utils.schedule_time import compute_next_run

if TYPE_CHECKING:
    from orchestrator.host.control import LocalControlPlane

logger = logging.getLogger(__name__)
KINDS = ("once", "interval", "cron")


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


class ScheduleManager:
    def __init__(self, plane: "LocalControlPlane") -> None:
        self.plane = plane
        self.store = plane.store

    def create(
        self,
        project_id: str,
        *,
        title: str = "",
        role_id: str = "",
        brief: str = "",
        kind: str = "interval",
        cron: str = "",
        interval_seconds: int = 0,
        objective_seq: int = 0,
        enabled: bool = True,
        skip_if_running: bool = True,
    ) -> Schedule:
        """Create and arm a schedule. Raises ``ValueError`` on a bad cron/interval or a missing target."""
        kind = kind if kind in KINDS else "interval"
        if not role_id.strip() and not objective_seq:
            raise ValueError("a schedule needs a role_id or an objective_seq")
        if role_id.strip():
            from orchestrator.crew.roles.registry import RoleRegistry

            if RoleRegistry.shared().get(role_id.strip()) is None:
                raise ValueError(f"unknown role {role_id.strip()!r}")
        sched = Schedule(
            id=uuid.uuid4().hex[:12], project_id=project_id, title=title.strip()[:255], role_id=role_id.strip(),
            brief=brief.strip(), kind=kind, cron=cron.strip()[:64], interval_seconds=max(0, int(interval_seconds or 0)),
            objective_seq=int(objective_seq or 0), enabled=enabled, skip_if_running=skip_if_running,
        )
        self.store.set_project(project_id)
        self._arm(sched)  # validates cron/interval (raises ValueError)
        self.store.put_schedule(sched)
        return sched

    def _arm(self, sched: Schedule, *, after: float | None = None) -> Schedule:
        """Set ``next_run_at`` from kind/cron/interval (a fired ``once`` schedule disables itself)."""
        now = after if after is not None else time.time()
        if sched.kind == "once" and sched.last_run_at:
            sched.next_run_at, sched.enabled = 0.0, False
            return sched
        # a ``once`` schedule that has not fired yet is due immediately (no anchor ⇒ "now")
        anchor = None if sched.kind == "once" else _dt(sched.last_run_at or sched.created_at or now)
        nxt = compute_next_run(
            kind=sched.kind, cron=sched.cron, interval_seconds=sched.interval_seconds,
            after=_dt(now), anchor=anchor,
        )
        sched.next_run_at = nxt.timestamp() if nxt else 0.0
        return sched

    def _rearm_and_save(self, sched: Schedule, *, after: float | None = None) -> None:
        try:
            self._arm(sched, after=after)
        except ValueError as exc:
            logger.warning("schedule %s arm failed: %s", sched.id, exc)
            sched.enabled, sched.next_run_at = False, 0.0
        self.store.patch_schedule(sched.id, next_run_at=sched.next_run_at, enabled=sched.enabled)

    def list(self, project_id: str = "") -> list[Schedule]:
        return self.store.list_schedules(project_id)

    def delete(self, schedule_id: str) -> bool:
        return self.store.delete_schedule(schedule_id)

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        sched = self.store.get_schedule(schedule_id)
        if sched is None:
            return False
        sched.enabled = bool(enabled)
        if enabled:
            self._rearm_and_save(sched)
        self.store.patch_schedule(schedule_id, enabled=sched.enabled)
        return True

    # ── firing ───────────────────────────────────────────────────────────────

    def _has_running(self, sched: Schedule) -> bool:
        if sched.last_job_id:
            last = self.store.get_job(sched.last_job_id)
            if last is not None and last.status not in TERMINAL:
                return True
        if sched.objective_seq:
            return any(j.status not in TERMINAL and j.objective_seq == sched.objective_seq
                       for j in self.store.list_jobs(sched.project_id))
        return False

    def fire(self, schedule_id: str, *, now: float | None = None, only_if_due: bool = False) -> JobState | None:
        """Start one job for a (due) schedule and advance ``next_run_at``. None if skipped.

        ``now`` is the logical clock (default: wall time) used for ``last_run_at`` and re-arming."""
        clock = now if now is not None else time.time()
        with self.store.lock(f"schedule-fire:{schedule_id}"):
            sched = self.store.get_schedule(schedule_id)
            if sched is None or not sched.enabled:
                return None
            if only_if_due and not (sched.next_run_at and sched.next_run_at <= clock):
                return None   # another scheduler already fired it this tick
            meta = self.store.get_project(sched.project_id)
            if meta.status != "active" or (meta.kind == "durable" and not meta.plan_accepted):
                return None
            if sched.skip_if_running and self._has_running(sched):
                self._rearm_and_save(sched, after=clock)
                return None
            job: JobState | None
            if sched.objective_seq:
                obj = next((o for o in self.store.get_objectives(sched.project_id) if o.seq == sched.objective_seq), None)
                if obj is None:
                    logger.warning("schedule %s: objective %s is gone", sched.id, sched.objective_seq)
                    self._rearm_and_save(sched, after=clock)
                    return None
                if obj.status in {"completed", "blocked", "cancelled"}:
                    self.store.patch_objective(sched.project_id, obj.seq, status="pending", note="")
                    obj = next(o for o in self.store.get_objectives(sched.project_id) if o.seq == obj.seq)
                job = self.plane.scheduler.create_run(sched.project_id, obj, extra=sched.brief)
            else:
                title = (sched.title or f"Scheduled {sched.role_id}").strip()[:255]
                job = self.plane.submit(project_id=sched.project_id, title=title,
                                        description=(sched.brief or title).strip(), role_ids=[sched.role_id])
            sched.last_run_at, sched.last_job_id = clock, (job.id if job else sched.last_job_id)
            self.store.patch_schedule(sched.id, last_run_at=clock, last_job_id=sched.last_job_id)
            if job is not None:
                try:
                    write_checkpoint(self.plane, job, reason="scheduled_run",
                                     extra={"schedule_id": sched.id, "schedule_kind": sched.kind})
                except Exception:  # noqa: BLE001
                    logger.warning("scheduled_run checkpoint failed for %s", job.id, exc_info=True)
            self._rearm_and_save(sched, after=clock)
            return job

    def fire_due(self, *, now: float | None = None, limit: int = 50) -> dict[str, Any]:
        """Fire every enabled schedule whose ``next_run_at`` has passed (oldest first)."""
        clock = now if now is not None else time.time()
        due = [s for s in self.store.list_schedules() if s.enabled and s.next_run_at and s.next_run_at <= clock][:limit]
        fired: list[str] = []
        skipped = 0
        for sched in due:
            try:
                job = self.fire(sched.id, now=clock, only_if_due=True)
            except Exception:  # noqa: BLE001
                logger.exception("fire schedule %s failed", sched.id)
                job = None
            if job is not None:
                fired.append(job.id)
            else:
                skipped += 1
        return {"ok": True, "due": len(due), "fired": fired, "skipped": skipped}

    def rearm_completed(self, project_id: str) -> int:
        """A durable project finished a cycle: scheduled objectives go back to ``pending`` for the next run."""
        meta = self.store.get_project(project_id)
        if meta.kind != "durable":
            return 0
        n = 0
        for sched in self.store.list_schedules(project_id):
            if not sched.enabled or not sched.objective_seq:
                continue
            obj = next((o for o in self.store.get_objectives(project_id) if o.seq == sched.objective_seq), None)
            if obj is not None and obj.status == "completed":
                # pending again, but held for the schedule: the scheduler must not release it on its own
                self.store.patch_objective(project_id, obj.seq, status="pending", note="scheduled")
                n += 1
            if not sched.next_run_at:
                self._rearm_and_save(sched)
        return n
