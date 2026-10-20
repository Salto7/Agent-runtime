"""Dramatiq actors: run a job, run a dream cycle, clean up a sandbox.

Start workers with ``dramatiq orchestrator.queue.tasks -Q orchestrator.jobs`` (or ``orchestrator worker``).
Importing this module configures the broker first, so actors bind to the right one.
"""

from __future__ import annotations

import logging
import threading

from orchestrator.queue.broker import QUEUE, ensure_broker

ensure_broker()

import dramatiq  # noqa: E402

from orchestrator import settings  # noqa: E402

logger = logging.getLogger(__name__)
_plane = None
_plane_lock = threading.Lock()


def worker_plane():
    """The per-process control plane used by actors (Redis state, Dramatiq dispatch)."""
    global _plane
    with _plane_lock:
        if _plane is None:
            from orchestrator.config import bootstrap
            from orchestrator.queue.plane import build_queue_plane

            bootstrap()
            from orchestrator.utils import llm_usage

            llm_usage.enable()
            _plane = build_queue_plane()
            try:
                _plane.start_stream_server()
            except OSError as exc:
                logger.warning("stream socket unavailable: %s", exc)
        return _plane


@dramatiq.actor(queue_name=QUEUE, max_retries=0, time_limit=settings.get_int("DRAMATIQ_TIME_LIMIT_MS", 6 * 60 * 60 * 1000))
def run_job(job_id: str, resume: bool = False, steer: str = "") -> None:
    """Claim the job (pending → running) and run one agent turn in its project sandbox."""
    worker_plane().execute(job_id, resume, steer)


@dramatiq.actor(queue_name=QUEUE, max_retries=0, time_limit=60 * 60 * 1000)
def dream_cycle(limit: int = 20) -> dict:
    """One dream cycle: improve due sessions, curate gists, synthesize playbooks, stage proposals."""
    from orchestrator.crew.dreaming import DreamingService

    svc = DreamingService.shared()
    svc.run_source = worker_plane().run_log
    report = svc.run_cycle(limit=limit)
    logger.info("dream cycle: %s", {k: report.get(k) for k in ("ok", "count", "errors")})
    return report


@dramatiq.actor(queue_name=QUEUE, max_retries=3, time_limit=120_000)
def cleanup_sandbox(project_id: str, runtime_id: str = "") -> None:
    """Remove a project's sandbox container (the worker holds the docker socket)."""
    from orchestrator.host.sandbox import ProjectSandbox

    result = ProjectSandbox.remove(str(project_id), runtime_id=runtime_id)
    logger.info("sandbox cleanup %s: %s", project_id, result.get("action"))
