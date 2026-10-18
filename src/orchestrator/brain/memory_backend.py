"""In-process reference implementation of the brain's storage protocols.

Zero setup, zero persistence across process restarts — this is what the
brain uses when a host app doesn't supply its own ``RoleStore`` /
``MessageStore`` / ``MemoryStore`` / ``ProposalStore``. It exists so the
brain is usable (and its own tests run) with nothing but Python installed;
a real app swaps in a database-backed implementation of the same four
protocols without the brain's logic changing at all.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from orchestrator.brain.protocols import (
    LessonRecord,
    Message,
    ProposalRecord,
    RoleRecord,
    utcnow,
)


class InMemoryRoleStore:
    def __init__(self) -> None:
        self._roles: dict[str, RoleRecord] = {}

    def list_roles(self) -> list[RoleRecord]:
        return sorted(self._roles.values(), key=lambda r: r.role_id)

    def get_role(self, role_id: str) -> RoleRecord | None:
        return self._roles.get(role_id)

    def upsert_role(self, role: RoleRecord) -> RoleRecord:
        role = replace(role, updated_at=utcnow())
        self._roles[role.role_id] = role
        return role

    def delete_role(self, role_id: str) -> None:
        self._roles.pop(role_id, None)


class InMemoryMessageStore:
    def __init__(self) -> None:
        self._messages: dict[str, Message] = {}

    def create_message(self, message: Message) -> Message:
        self._messages[message.id] = message
        return message

    def list_messages(
        self, *, job_id: str, since: datetime | None = None, unread_only: bool = False
    ) -> list[Message]:
        out = [
            m
            for m in self._messages.values()
            if m.recipient_job_id == job_id or m.sender_job_id == job_id
        ]
        if since is not None:
            out = [m for m in out if m.created_at >= since]
        if unread_only:
            out = [m for m in out if m.recipient_job_id == job_id and m.read_at is None]
        return sorted(out, key=lambda m: m.created_at)

    def mark_read(self, message_id: str) -> None:
        m = self._messages.get(message_id)
        if m is not None:
            self._messages[message_id] = replace(m, read_at=utcnow())

    def count_between(self, job_a: str, job_b: str) -> int:
        pair = {job_a, job_b}
        return sum(
            1
            for m in self._messages.values()
            if {m.sender_job_id, m.recipient_job_id} == pair
        )


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._lessons: dict[str, LessonRecord] = {}

    def save_lesson(self, lesson: LessonRecord) -> LessonRecord:
        self._lessons[lesson.name] = lesson
        return lesson

    def list_lessons(self, *, prefix: str = "") -> list[LessonRecord]:
        rows = [l for l in self._lessons.values() if l.name.startswith(prefix)]
        return sorted(rows, key=lambda l: l.created_at, reverse=True)

    def save_playbook(self, name: str, description: str, body: str) -> None:
        # A playbook is a synthesized lesson — same collection, same
        # list_lessons() path, so callers never query two different stores.
        self.save_lesson(LessonRecord(name=name, task_kind="playbook", description=description, body=body))


class InMemoryProposalStore:
    def __init__(self) -> None:
        self._proposals: dict[str, ProposalRecord] = {}

    def create_proposal(self, proposal: ProposalRecord) -> ProposalRecord:
        pid = proposal.id or uuid.uuid4().hex
        proposal = replace(proposal, id=pid)
        self._proposals[pid] = proposal
        return proposal

    def list_proposals(self, *, status: str = "pending") -> list[ProposalRecord]:
        rows = list(self._proposals.values())
        if status != "all":
            rows = [p for p in rows if p.status == status]
        return sorted(rows, key=lambda p: p.created_at, reverse=True)

    def get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        return self._proposals.get(proposal_id)

    def decide_proposal(self, proposal_id: str, status: str) -> ProposalRecord:
        current = self._proposals[proposal_id]
        updated = replace(current, status=status, decided_at=utcnow())
        self._proposals[proposal_id] = updated
        return updated

    def find_pending(self, *, kind: str, target_id: str) -> ProposalRecord | None:
        for p in self._proposals.values():
            if p.status == "pending" and p.kind == kind and p.target_id == target_id:
                return p
        return None
