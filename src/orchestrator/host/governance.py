"""The resident governance PM.

One per project, born ``idle`` and tied to no objective. It costs nothing while parked (event-driven: no poll, no
LLM), receives spawn/progress informs (no wake) and wakes only on escalations (a blocked objective, a scope
change). On wake it reads its inbox, governs (redirect, reprioritize, update objectives, replan, ``ask_operator``),
and re-parks. It is retired when the engagement finishes.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from orchestrator.host.state import TERMINAL, JobState

if TYPE_CHECKING:
    from orchestrator.host.control import LocalControlPlane

logger = logging.getLogger(__name__)

PROFILE = "governance"
NOTE = "governing engagement"
BRIEF = (
    "Resident engagement governor. You receive spawn and progress informs from the crew and wake only when an "
    "agent escalates a block or a scope/plan change. On wake, read your whole inbox, govern (redirect, "
    "reprioritize, update objectives, replan, or ask_operator), then end your turn — you park again automatically."
)


class GovernanceManager:
    def __init__(self, plane: "LocalControlPlane") -> None:
        self.plane = plane
        self.store = plane.store

    def governor(self, project_id: str) -> JobState | None:
        """The live resident governance PM for ``project_id`` (or None)."""
        live = [j for j in self.store.list_jobs(project_id) if j.profile == PROFILE and j.status not in TERMINAL]
        return live[0] if live else None

    def ensure(self, project_id: str) -> JobState | None:
        """Create the single resident idle governance PM if none is live (needs a manager role)."""
        from orchestrator.crew.roles.registry import RoleRegistry

        manager = RoleRegistry.shared().manager()
        if manager is None:
            return None
        with self.store.lock(f"governor:{project_id}"):
            existing = self.governor(project_id)
            if existing is not None:
                return existing
            job = JobState(
                id=uuid.uuid4().hex[:12],
                project_id=project_id, title="Governance PM", description=BRIEF, role_ids=[manager.id],
                status="idle", idle_note=NOTE, profile=PROFILE,
            )
            self.store.put_job(job)
            return job

    def notify(self, project_id: str, *, text: str, from_job_id: str = "", escalate: bool = False) -> bool:
        """Inform (no wake) or escalate (wake) the governor. Returns whether a message was sent."""
        pm = self.governor(project_id)
        if pm is None or from_job_id == pm.id:
            return False
        sender = from_job_id or "system"
        self.plane.hub.send(
            sender_job_id=sender, recipient_job_id=pm.id, kind="request" if escalate else "inform",
            content=text, wake=self.plane.wake if escalate else None, backstop=self.plane.interim_backstop,
        )
        return True

    def try_notify(self, project_id: str, **kwargs) -> None:
        """Best-effort :meth:`notify` — governance must never break the caller."""
        try:
            self.notify(project_id, **kwargs)
        except Exception:  # noqa: BLE001
            logger.debug("governor notify failed", exc_info=True)

    def repark(self, job_id: str) -> None:
        """After a governor turn: back to idle (with a checkpoint), and re-wake if mail arrived meanwhile."""
        from orchestrator.host.checkpoints import write_checkpoint

        job = self.store.get_job(job_id)
        if job is None:
            return
        self.store.patch_job(job_id, status="idle", idle_note=NOTE, error="")
        write_checkpoint(self.plane, self.store.get_job(job_id) or job, reason="idle")
        if self.plane._unread(job_id):
            self.plane.wake(job_id)

    def cancel(self, project_id: str) -> int:
        """Retire the governor when the engagement finishes."""
        n = 0
        for j in self.store.list_jobs(project_id):
            if j.profile != PROFILE or j.status in TERMINAL:
                continue
            if self.plane.cancel(j.id):   # idle, or woken by an escalation and still running/pending
                self.store.patch_job(j.id, error="Engagement finished", idle_note="")
                n += 1
        return n
