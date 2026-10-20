"""Dramatiq broker setup (Redis by default; an in-memory stub for tests/dev)."""

from __future__ import annotations

import logging

import dramatiq
from dramatiq.brokers.stub import StubBroker

from orchestrator import settings

logger = logging.getLogger(__name__)

QUEUE = "orchestrator.jobs"
_configured = False


def redis_url() -> str:
    return settings.get_str("REDIS_URL", "redis://localhost:6379/0")


def ensure_broker() -> dramatiq.Broker:
    """Install the process-wide Dramatiq broker once, *before* actors are declared.

    ``ORCHESTRATOR_BROKER=stub`` selects dramatiq's in-memory ``StubBroker`` (tests); anything
    else uses ``RedisBroker(REDIS_URL)`` so the worker never falls back to dramatiq's
    localhost default.
    """
    global _configured
    if _configured:
        return dramatiq.get_broker()
    if settings.get_str("ORCHESTRATOR_BROKER", "redis").lower() == "stub":
        broker: dramatiq.Broker = StubBroker()
        broker.emit_after("process_boot")
    else:
        from dramatiq.brokers.redis import RedisBroker

        broker = RedisBroker(url=redis_url())
    dramatiq.set_broker(broker)
    _configured = True
    logger.info("dramatiq broker: %s", type(broker).__name__)
    return broker


