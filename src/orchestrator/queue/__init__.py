"""Durable execution: Dramatiq (Redis) workers + Redis-shared control-plane state.

- ``broker`` — Dramatiq broker setup (``REDIS_URL``; ``ORCHESTRATOR_BROKER=stub`` for tests)
- ``tasks`` — actors ``run_job`` / ``dream_cycle`` / ``cleanup_sandbox`` (``dramatiq orchestrator.queue.tasks``)
- ``plane`` — ``build_queue_plane()``: ``LocalControlPlane`` on Redis state + Dramatiq executor
- ``redis_state`` — ``RedisStateStore`` / ``RedisMessageStore``
- ``scheduler`` — periodic dream cycles

Importing this package is cheap; ``tasks`` (which needs a broker) is imported on use.
"""

from orchestrator.queue.redis_state import RedisMessageStore, RedisStateStore

__all__ = ["RedisMessageStore", "RedisStateStore", "build_queue_plane"]


def build_queue_plane(*args, **kwargs):
    from orchestrator.queue.plane import build_queue_plane as _build

    return _build(*args, **kwargs)
