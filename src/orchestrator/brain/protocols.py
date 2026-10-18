"""Storage contracts for the brain (roles, A2A, memory/dream).

The brain's logic (``roles.py``, ``a2a.py``, ``dream.py``) never touches a
database directly — it calls these protocols. ``memory_backend.py`` ships an
in-process reference implementation of all four so the brain works with zero
setup; a host app (Django, SQLAlchemy, anything) swaps in its own by
implementing the same methods. Nothing here imports Django, an ORM, or any
specific storage engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Roles ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoleRecord:
    """One role: an identity a job can run as, plus its skill bindings."""

    role_id: str
    kind: str = "worker"  # e.g. "lead" | "worker" — host-defined vocabulary
    aliases: tuple[str, ...] = ()
    goal: str = ""
    backstory: str = ""
    skills: tuple[str, ...] = ()
    protected: bool = False
    updated_at: datetime = field(default_factory=utcnow)


@runtime_checkable
class RoleStore(Protocol):
    def list_roles(self) -> list[RoleRecord]: ...
    def get_role(self, role_id: str) -> RoleRecord | None: ...
    def upsert_role(self, role: RoleRecord) -> RoleRecord: ...
    def delete_role(self, role_id: str) -> None: ...


# ── A2A messaging ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Message:
    """One directed message between two jobs. Never a broadcast."""

    id: str
    sender_job_id: str
    recipient_job_id: str
    kind: str  # "request" | "inform" | "handoff" | ...
    content: str
    created_at: datetime = field(default_factory=utcnow)
    read_at: datetime | None = None
    reply_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MessageStore(Protocol):
    def create_message(self, message: Message) -> Message: ...
    def list_messages(
        self, *, job_id: str, since: datetime | None = None, unread_only: bool = False
    ) -> list[Message]: ...
    def mark_read(self, message_id: str) -> None: ...
    def count_between(self, job_a: str, job_b: str) -> int: ...


# ── Memory / lessons / playbooks ────────────────────────────────────────


@dataclass(frozen=True)
class LessonRecord:
    """One curated gist: what one finished job's run taught, stripped of
    any target-identifying detail by the curator before it ever gets here."""

    name: str
    task_kind: str
    description: str
    body: str
    role_id: str = ""
    created_at: datetime = field(default_factory=utcnow)


@runtime_checkable
class MemoryStore(Protocol):
    def save_lesson(self, lesson: LessonRecord) -> LessonRecord: ...
    def list_lessons(self, *, prefix: str = "") -> list[LessonRecord]: ...
    def save_playbook(self, name: str, description: str, body: str) -> None: ...


# ── Pending proposals (review gate) ─────────────────────────────────────


@dataclass(frozen=True)
class ProposalRecord:
    """A suggested role/skill change, staged for human review.

    Never applied automatically — ``dream.apply_proposal`` is the only path
    from here to a live ``RoleStore``/skill change, and it requires an
    explicit ``decision``.
    """

    id: str
    kind: str  # "role" | "skill"
    action: str  # "create" | "update"
    target_id: str
    title: str
    rationale: str
    payload: dict[str, Any]
    status: str = "pending"  # "pending" | "approved" | "rejected"
    created_at: datetime = field(default_factory=utcnow)
    decided_at: datetime | None = None


@runtime_checkable
class ProposalStore(Protocol):
    def create_proposal(self, proposal: ProposalRecord) -> ProposalRecord: ...
    def list_proposals(self, *, status: str = "pending") -> list[ProposalRecord]: ...
    def get_proposal(self, proposal_id: str) -> ProposalRecord | None: ...
    def decide_proposal(self, proposal_id: str, status: str) -> ProposalRecord: ...
    def find_pending(self, *, kind: str, target_id: str) -> ProposalRecord | None: ...
