"""Library CLI.

    orchestrator worker [--threads N]   Dramatiq worker (jobs run in project sandboxes)
    orchestrator scheduler              periodic dream cycles (enqueues ``dream_cycle``)
    orchestrator <anything else>        the thin client (list-roles, plan, run, dream, memory-health, …)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _worker(argv: list[str]) -> int:
    from orchestrator import settings

    threads = settings.get_int("DRAMATIQ_MAX_THREADS", 4)
    if "--threads" in argv:
        threads = int(argv[argv.index("--threads") + 1])
    from orchestrator.queue.broker import QUEUE

    os.execvp(
        sys.executable,
        [sys.executable, "-m", "dramatiq", "orchestrator.queue.tasks",
         "--processes", "1", "--threads", str(max(1, threads)), "-Q", QUEUE],
    )
    return 0  # pragma: no cover


def _scheduler() -> int:
    import logging

    from orchestrator.queue.scheduler import run_forever

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_forever()
    return 0


def _thin_client(argv: list[str] | None) -> int:
    import importlib.util

    for base in (Path(__file__).resolve().parents[2], Path.cwd()):
        client = base / "examples" / "thin_client.py"
        if client.is_file():
            spec = importlib.util.spec_from_file_location("orchestrator_thin_client", client)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return int(mod.main(argv))
    from orchestrator import Orchestrator

    orch = Orchestrator()
    print("roles:", len(orch.list_roles()), "skills:", len(orch.list_skills()), "tools:", len(orch.list_tools()))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["worker"]:
        return _worker(args[1:])
    if args[:1] == ["scheduler"]:
        return _scheduler()
    return _thin_client(args or None)


if __name__ == "__main__":
    raise SystemExit(main())
