"""Agent-to-agent messaging (A2A): directed messages with caps and wake hooks.

Roles live in ``orchestrator.catalog`` / ``orchestrator.crew.roles``; memory and
the dream cycle in ``orchestrator.memory`` / ``orchestrator.crew.dreaming``.
"""

from orchestrator.brain.a2a import A2AHub
from orchestrator.brain.memory_backend import InMemoryMessageStore
from orchestrator.brain.protocols import Message, MessageStore

__all__ = ["A2AHub", "InMemoryMessageStore", "Message", "MessageStore"]
