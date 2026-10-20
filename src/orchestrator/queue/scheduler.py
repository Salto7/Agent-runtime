"""Periodic housekeeping, one tick every ``JOB_REAP_S`` seconds: fire due schedules, reap jobs whose worker died
(no heartbeat for ``JOB_STALE_S``), and every ``DREAMING_INTERVAL_S`` enqueue a ``dream_cycle``.

Dramatiq has no cron, so this is a tiny loop run as its own service
(``python -m orchestrator.queue.scheduler`` / ``orchestrator scheduler``).
"""

from __future__ import annotations

import logging
import signal
import time

from orchestrator import settings

logger = logging.getLogger("orchestrator.scheduler")


def run_forever(interval_s: float | None = None, *, once: bool = False) -> None:
    from orchestrator.queue.tasks import dream_cycle, worker_plane

    interval = float(interval_s if interval_s is not None else settings.get_float("DREAMING_INTERVAL_S", 900))
    reap_every = max(1.0, settings.get_float("JOB_REAP_S", 30))
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    logger.info("scheduler: dream every %.0fs, reap stale jobs every %.0fs", interval, reap_every)
    next_dream = 0.0
    while not stop["now"]:
        now = time.monotonic()
        try:
            plane = worker_plane()
            plane.reap_stale()
            fired = plane.schedules.fire_due().get("fired")
            if fired:
                logger.info("fired schedules → jobs %s", fired)
        except Exception:  # noqa: BLE001 — keep the loop alive
            logger.exception("scheduler tick failed")
        if now >= next_dream and settings.get_bool("DREAMING_ENABLED", True):
            dream_cycle.send()
            logger.info("enqueued dream_cycle")
            next_dream = now + interval
        if once:
            return
        deadline = time.monotonic() + reap_every
        while time.monotonic() < deadline and not stop["now"]:
            time.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_forever()
