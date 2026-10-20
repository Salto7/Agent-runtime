"""In-process ``MessageStore`` — the zero-setup default for ``A2AHub``.

No persistence across restarts; a host that needs durable messages passes its
own ``MessageStore`` to ``A2AHub(store=...)``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from orchestrator.brain.protocols import Message, utcnow


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
