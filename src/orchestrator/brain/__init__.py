"""The brain: roles, directed agent-to-agent messaging, and Dream (memory
consolidation + role-change proposals) for a multi-agent runtime.

Framework-agnostic and storage-agnostic. Logic lives in ``roles``, ``a2a``,
and ``dream``/``scheduler``; all four talk only to the protocols in
``protocols`` (``RoleStore``, ``MessageStore``, ``MemoryStore``,
``ProposalStore``). ``memory_backend`` ships an in-process reference
implementation of each so this package works standalone; a host app swaps
in its own (a database, an existing ORM, anything) without this package's
code changing.

A role/skill change proposed by Dream is never applied automatically —
``dream.apply_proposal`` is the only path from a pending ``ProposalRecord``
to a live role, and it requires an explicit human decision.
"""

from __future__ import annotations

from orchestrator.brain.a2a import A2AHub
from orchestrator.brain.protocols import (
    LessonRecord,
    Message,
    MessageStore,
    MemoryStore,
    ProposalRecord,
    ProposalStore,
    RoleRecord,
    RoleStore,
)
from orchestrator.brain.roles import RoleRegistry
from orchestrator.brain.scheduler import DreamCycleResult, tick

__all__ = [
    "A2AHub",
    "DreamCycleResult",
    "LessonRecord",
    "Message",
    "MessageStore",
    "MemoryStore",
    "ProposalRecord",
    "ProposalStore",
    "RoleRecord",
    "RoleRegistry",
    "RoleStore",
    "tick",
]
