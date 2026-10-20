"""Build a control plane whose state lives in Redis and whose jobs run on Dramatiq workers."""

from __future__ import annotations

from typing import Any

from orchestrator.brain.a2a import A2AHub
from orchestrator.host.control import LocalControlPlane
from orchestrator import settings
from orchestrator.queue.broker import redis_url
from orchestrator.queue.redis_state import RedisMessageStore, RedisStateStore


class DramatiqExecutor:
    """``executor`` for ``LocalControlPlane``: enqueue ``run_job`` instead of running a thread."""

    def __init__(self) -> None:
        self.plane: LocalControlPlane | None = None

    def dispatch(self, job_id: str, resume: bool = False, steer: str = "", delay_ms: int = 0) -> None:
        from orchestrator.queue.tasks import run_job

        if delay_ms > 0:
            run_job.send_with_options(args=(job_id, resume, steer), delay=delay_ms)
        else:
            run_job.send(job_id, resume, steer)


def build_queue_plane(
    url: str | None = None, *, prefix: str = "", store: Any = None, hub: Any = None, **kwargs: Any
) -> LocalControlPlane:
    """Control plane backed by Redis (state + A2A) with the Dramatiq executor.

    The same call works in the web/CLI process (to submit and observe) and in workers (to execute):
    both see the same jobs, findings, objectives and messages.
    """
    url = url or redis_url()
    prefix = prefix or settings.get_str("ORCHESTRATOR_REDIS_PREFIX", "orch")
    max_recipients = max(1, settings.get_int("A2A_MAX_RECIPIENTS", 5))
    max_pair = max(1, settings.get_int("A2A_MAX_MESSAGES_PER_PAIR", 40))
    return LocalControlPlane(
        store=store or RedisStateStore(url, prefix=prefix),
        hub=hub or A2AHub(store=RedisMessageStore(url, prefix=prefix), max_recipients=max_recipients,
                              max_messages_per_pair=max_pair),
        executor=DramatiqExecutor(),
        **kwargs,
    )
