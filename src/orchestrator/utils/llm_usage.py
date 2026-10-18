"""LLM cost/token telemetry — one litellm callback, no per-call-site plumbing.

Any call through ``litellm`` (directly, or wrapped by an agent framework's
own ``LLM`` class) is captured by registering one
``litellm.success_callback``/``failure_callback`` pair, including call
sites this module never imports.

Call sites tag *why* a call happened with :func:`purpose` (a context
manager); job runs can be tagged from ``JobEnv`` (job/project/role id) if
the host binds one. A call with no ``purpose()`` and no job context is
logged as ``"unlabeled"`` — a sign a call site still needs tagging, not a
bug.

Storage is injected: ``register(sink)`` takes any ``UsageSink`` (an
in-memory one ships here for tests/standalone use; a host wires in its own
DB-backed one). A write that fails is logged and dropped — telemetry must
never break a real call.

litellm runs ``success_callback``/``failure_callback`` off the calling
thread, so a row typically lands a moment after ``complete()``/``kickoff()``
returns — never assume a row is already written immediately after a call,
e.g. in a script that calls then exits.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_purpose: ContextVar[str] = ContextVar("llm_usage_purpose", default="")
_registered = False
_sink: "UsageSink | None" = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LlmUsageRecord:
    purpose: str
    model: str
    ok: bool
    project_id: str = ""
    job_id: str = ""
    role_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str = ""
    created_at: datetime = field(default_factory=utcnow)


@runtime_checkable
class UsageSink(Protocol):
    def record(self, row: LlmUsageRecord) -> None: ...


class InMemoryUsageSink:
    """Reference ``UsageSink`` — keeps everything in a list, for tests/standalone use."""

    def __init__(self) -> None:
        self.records: list[LlmUsageRecord] = []

    def record(self, row: LlmUsageRecord) -> None:
        self.records.append(row)

    def summary(self, *, since_hours: float = 24.0) -> list[dict[str, Any]]:
        """Cost/tokens/call-count grouped by purpose, most expensive first."""
        from datetime import timedelta

        cutoff = utcnow() - timedelta(hours=max(0.0, since_hours))
        totals: dict[str, dict[str, Any]] = {}
        for row in self.records:
            if row.created_at < cutoff:
                continue
            bucket = totals.setdefault(
                row.purpose,
                {
                    "purpose": row.purpose,
                    "calls": 0,
                    "failed": 0,
                    "cost_usd": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
            )
            bucket["calls"] += 1
            bucket["failed"] += 0 if row.ok else 1
            bucket["cost_usd"] += row.cost_usd
            bucket["prompt_tokens"] += row.prompt_tokens
            bucket["completion_tokens"] += row.completion_tokens
        return sorted(totals.values(), key=lambda b: b["cost_usd"], reverse=True)


@contextmanager
def purpose(name: str) -> Iterator[None]:
    """Tag LLM calls made inside this block with ``name`` for cost reporting.

    Nests: an inner ``purpose()`` wins for calls made inside it, and the outer
    tag resumes for calls made after it exits.
    """
    token: Token = _purpose.set(str(name or "").strip() or "unlabeled")
    try:
        yield
    finally:
        _purpose.reset(token)


def current_purpose() -> str:
    return _purpose.get()


def _job_context() -> dict[str, str]:
    try:
        from orchestrator.utils.job_env import JobEnv
    except Exception:  # noqa: BLE001
        return {}
    return {
        "job_id": JobEnv.get("ORCHESTRATOR_JOB_ID"),
        "project_id": JobEnv.get("ORCHESTRATOR_PROJECT_ID"),
        "role_id": JobEnv.get("ORCHESTRATOR_ROLE_ID"),
    }


def _resolve_cost(kwargs: dict[str, Any], response: Any) -> float:
    """``response_cost`` litellm already computed, else ask it directly."""
    direct = kwargs.get("response_cost")
    if isinstance(direct, (int, float)):
        return float(direct)
    hidden = getattr(response, "hidden_params", None)
    hidden_cost = hidden.get("response_cost") if isinstance(hidden, dict) else None
    if isinstance(hidden_cost, (int, float)):
        return float(hidden_cost)
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:  # noqa: BLE001 — unknown/custom model pricing, not fatal
        return 0.0


def _usage_of(response: Any) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    getter = usage.get if isinstance(usage, dict) else lambda k: getattr(usage, k, None)
    prompt = int(getter("prompt_tokens") or 0) if usage else 0
    completion = int(getter("completion_tokens") or 0) if usage else 0
    total = int(getter("total_tokens") or 0) if usage else prompt + completion
    return prompt, completion, total


def _write(row: LlmUsageRecord) -> None:
    if _sink is None:
        return
    try:
        _sink.record(row)
    except Exception:  # noqa: BLE001 — telemetry must never break a real call
        logger.debug("llm usage sink write failed", exc_info=True)


def _log_success(kwargs: dict[str, Any], response: Any, start_time, end_time) -> None:
    prompt_t, completion_t, total_t = _usage_of(response)
    duration_ms = int(max(0.0, (end_time - start_time).total_seconds()) * 1000)
    ctx = _job_context()
    _write(
        LlmUsageRecord(
            purpose=current_purpose() or "unlabeled",
            model=str(kwargs.get("model") or ""),
            project_id=ctx.get("project_id", "")[:36],
            job_id=ctx.get("job_id", "")[:36],
            role_id=ctx.get("role_id", "")[:64],
            prompt_tokens=prompt_t,
            completion_tokens=completion_t,
            total_tokens=total_t,
            cost_usd=_resolve_cost(kwargs, response),
            duration_ms=duration_ms,
            ok=True,
        )
    )


def _log_failure(kwargs: dict[str, Any], response: Any, start_time, end_time) -> None:
    del response
    duration_ms = int(max(0.0, (end_time - start_time).total_seconds()) * 1000)
    ctx = _job_context()
    error = str(kwargs.get("exception") or "")[:300]
    _write(
        LlmUsageRecord(
            purpose=current_purpose() or "unlabeled",
            model=str(kwargs.get("model") or ""),
            project_id=ctx.get("project_id", "")[:36],
            job_id=ctx.get("job_id", "")[:36],
            role_id=ctx.get("role_id", "")[:64],
            duration_ms=duration_ms,
            ok=False,
            error=error,
        )
    )


def register(sink: UsageSink) -> None:
    """Install the litellm callbacks once per process. Safe to call repeatedly.

    Calling again with a different ``sink`` swaps the sink future writes go
    to, without re-registering the callbacks with litellm.
    """
    global _registered, _sink
    _sink = sink
    if _registered:
        return
    try:
        import litellm
    except Exception:  # noqa: BLE001
        logger.warning("llm usage tracking skipped: litellm unavailable")
        return
    litellm.success_callback = [*(litellm.success_callback or []), _log_success]
    litellm.failure_callback = [*(litellm.failure_callback or []), _log_failure]
    _registered = True
    logger.info("llm usage tracking registered (litellm success/failure callbacks)")
