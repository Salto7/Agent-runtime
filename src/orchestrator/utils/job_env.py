"""Job-scoped environment via ContextVar (safe under Dramatiq / tool threads).

Worker threads must not share process-global ``os.environ`` for job fields.
``JobEnv.bind`` / ``overlay`` store values. The bound runtime session reads
them through the env lookup the control plane installs. Stream emitters
read via ``JobEnv.get`` (ContextVar first, then ``os.environ``).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

_job_env: ContextVar[dict[str, str] | None] = ContextVar("job_env", default=None)


class JobEnv:
    """Thread/context-local ORCHESTRATOR_* (and related) values for one job run."""

    @classmethod
    def current(cls) -> dict[str, str]:
        raw = _job_env.get()
        return dict(raw) if raw else {}

    @classmethod
    def bind(cls, env: dict[str, str] | None) -> Token:
        clean = {
            str(k): str(v)
            for k, v in (env or {}).items()
            if str(k).strip() and v is not None and str(v) != ""
        }
        return _job_env.set(clean)

    @classmethod
    def reset(cls, token: Token) -> None:
        _job_env.reset(token)

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        """Prefer context map; fall back to process env (container / CLI)."""
        cur = _job_env.get()
        if cur is not None and key in cur:
            return str(cur.get(key) or default)
        return (os.environ.get(key) or default).strip() or default

    @classmethod
    @contextmanager
    def overlay(cls, updates: dict[str, str]) -> Iterator[None]:
        """Temporarily merge keys for the current context (role id/command)."""
        merged = dict(cls.current())
        for k, v in (updates or {}).items():
            if v is None or str(v) == "":
                merged.pop(str(k), None)
            else:
                merged[str(k)] = str(v)
        token = cls.bind(merged)
        try:
            yield
        finally:
            cls.reset(token)
