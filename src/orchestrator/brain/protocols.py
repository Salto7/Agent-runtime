"""Storage contract for agent-to-agent messaging.

``A2AHub`` never touches a database directly — it calls ``MessageStore``.
``memory_backend.InMemoryMessageStore`` is the zero-setup default; a host swaps
in its own store (a DB, a queue) by implementing the same four methods.
Roles, skills and memory live in ``orchestrator.catalog`` / ``orchestrator.memory``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
