"""Agent-to-agent messaging: directed only, never a broadcast.

A message is always addressed to exactly one job id (``multisend`` loops
``send`` per recipient, it does not introduce a broadcast primitive). A
per-pair message cap stops two agents from looping each other into a
runaway exchange. Resuming a finished job on message arrival is a host
concern — pass a ``wake`` callback to ``send``/``multisend`` rather than
this module assuming anything about how your runtime resumes work.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from typing import Any

from orchestrator.brain.memory_backend import InMemoryMessageStore
from orchestrator.brain.protocols import Message, MessageStore
from orchestrator.utils.service import SharedService

DEFAULT_MAX_MESSAGES_PER_PAIR = 40
DEFAULT_MAX_RECIPIENTS = 5


class A2AHub(SharedService):
    def __init__(
        self,
        *,
        store: MessageStore | None = None,
        max_messages_per_pair: int = DEFAULT_MAX_MESSAGES_PER_PAIR,
        max_recipients: int = DEFAULT_MAX_RECIPIENTS,
    ) -> None:
        self._store = store or InMemoryMessageStore()
        self.max_messages_per_pair = max_messages_per_pair
        self.max_recipients = max_recipients

    def send(
        self,
        *,
        sender_job_id: str,
        recipient_job_id: str,
        kind: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        wake: Callable[[str], None] | None = None,
    ) -> Message:
        """Send one directed message. Raises ``ValueError`` on self-message
        or a per-pair cap hit — never silently drops or broadcasts instead."""
        if not sender_job_id or not recipient_job_id:
            raise ValueError("sender_job_id and recipient_job_id are required")
        if sender_job_id == recipient_job_id:
            raise ValueError("a job cannot message itself")
        count = self._store.count_between(sender_job_id, recipient_job_id)
        if count >= self.max_messages_per_pair:
            raise ValueError(
                f"message cap reached between {sender_job_id!r} and "
                f"{recipient_job_id!r} ({self.max_messages_per_pair})"
            )
        message = self._store.create_message(
            Message(
                id=uuid.uuid4().hex,
                sender_job_id=sender_job_id,
                recipient_job_id=recipient_job_id,
                kind=kind,
                content=content,
                reply_to=reply_to,
                metadata=dict(metadata or {}),
            )
        )
        if wake is not None:
            wake(recipient_job_id)
        return message

    def multisend(
        self,
        *,
        sender_job_id: str,
        recipient_job_ids: Iterable[str],
        kind: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        wake: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Send the same message to several recipients, capped and validated
        up front — either every recipient is attempted or none are, and
        each failure is reported individually rather than aborting the rest."""
        ids = list(dict.fromkeys(rid for rid in recipient_job_ids if rid))
        if not ids:
            raise ValueError("recipient_job_ids is empty")
        if len(ids) > self.max_recipients:
            raise ValueError(
                f"multisend is capped at {self.max_recipients} recipients, got {len(ids)}"
            )
        sent: list[Message] = []
        failed: dict[str, str] = {}
        for rid in ids:
            try:
                sent.append(
                    self.send(
                        sender_job_id=sender_job_id,
                        recipient_job_id=rid,
                        kind=kind,
                        content=content,
                        reply_to=reply_to,
                        metadata=metadata,
                        wake=wake,
                    )
                )
            except ValueError as exc:
                failed[rid] = str(exc)
        return {"sent": sent, "failed": failed}

    def inbox(self, job_id: str, *, unread_only: bool = False) -> list[Message]:
        return self._store.list_messages(job_id=job_id, unread_only=unread_only)

    def mark_read(self, message_id: str) -> None:
        self._store.mark_read(message_id)

    def status(self, job_id: str) -> dict[str, int]:
        """Delivery/read counters for one job — what message_status reports."""
        msgs = self._store.list_messages(job_id=job_id)
        received = [m for m in msgs if m.recipient_job_id == job_id]
        sent = [m for m in msgs if m.sender_job_id == job_id]
        return {
            "sent": len(sent),
            "received": len(received),
            "unread": sum(1 for m in received if m.read_at is None),
        }
