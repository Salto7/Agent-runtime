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


class SqliteUsageSink:
    """Default durable sink: rows in the platform SQLite DB (``LlmCallLog``), queryable via :meth:`summary`."""

    def record(self, row: LlmUsageRecord) -> None:
        import uuid

        from orchestrator.memory.models import LlmCallLog

        LlmCallLog(
            id=uuid.uuid4().hex, purpose=row.purpose, model=row.model, ok=row.ok, project_id=row.project_id,
            job_id=row.job_id, role_id=row.role_id, prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens, total_tokens=row.total_tokens, cost_usd=row.cost_usd,
            duration_ms=row.duration_ms, error=row.error, created_at=row.created_at.isoformat(),
        ).save(force_insert=True)

    def summary(self, *, since_hours: float = 24.0, project_id: str = "") -> list[dict[str, Any]]:
        """Cost/tokens/call-count grouped by purpose, most expensive first."""
        from datetime import timedelta

        from orchestrator.memory.models import LlmCallLog

        cutoff = (utcnow() - timedelta(hours=max(0.0, since_hours))).isoformat()
        totals: dict[str, dict[str, Any]] = {}
        for r in LlmCallLog.objects.all():
            if r.created_at < cutoff or (project_id and r.project_id != project_id):
                continue
            b = totals.setdefault(r.purpose, {"purpose": r.purpose, "calls": 0, "failed": 0, "cost_usd": 0.0,
                                              "prompt_tokens": 0, "completion_tokens": 0})
            b["calls"] += 1
            b["failed"] += 0 if r.ok else 1
            b["cost_usd"] += float(r.cost_usd or 0)
            b["prompt_tokens"] += int(r.prompt_tokens or 0)
            b["completion_tokens"] += int(r.completion_tokens or 0)
        return sorted(totals.values(), key=lambda b: b["cost_usd"], reverse=True)


def enable() -> UsageSink:
    """Register the default ``SqliteUsageSink`` (unless ``LLM_USAGE_ENABLED=false`` or a sink is already set)."""
    from orchestrator import settings

    if _sink is not None or not settings.get_bool("LLM_USAGE_ENABLED", True):
        return _sink or InMemoryUsageSink()
    sink = SqliteUsageSink()
    register(sink)
    return sink


def summary(*, since_hours: float = 24.0, project_id: str = "") -> list[dict[str, Any]]:
    """Usage grouped by purpose (from the registered sink; ``[]`` when none supports summaries)."""
    sink = _sink
    return sink.summary(since_hours=since_hours, project_id=project_id) if isinstance(sink, SqliteUsageSink) else (
        sink.summary(since_hours=since_hours) if hasattr(sink, "summary") else [])


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


def record_crew_usage(llms: Any, *, model: str = "") -> int:
    """Record the calls of CrewAI *native-provider* LLM objects (they bypass the LiteLLM callbacks).

    Call after a kickoff, inside the job's ``bind_job`` / ``purpose`` blocks, with the agents' ``llm`` objects.
    One row per request (tokens split evenly); LiteLLM-backed ``crewai.LLM`` objects are skipped — the callbacks
    already recorded those. Returns the number of rows written.
    """
    ctx = _job_context()
    seen: set[int] = set()
    written = 0
    for llm in llms or []:
        if llm is None or isinstance(llm, str) or id(llm) in seen or not hasattr(llm, "get_token_usage_summary"):
            continue
        seen.add(id(llm))
        if type(llm).__module__ == "crewai.llm":
            continue
        try:
            u = llm.get_token_usage_summary()
            n = int(getattr(u, "successful_requests", 0) or 0)
            prompt, completion = int(u.prompt_tokens or 0), int(u.completion_tokens or 0)
        except Exception:  # noqa: BLE001
            continue
        if n <= 0:
            continue
        name = str(getattr(llm, "model", "") or model)
        try:
            import litellm

            p_cost, c_cost = litellm.cost_per_token(model=name, prompt_tokens=prompt, completion_tokens=completion)
            cost = float(p_cost + c_cost)
        except Exception:  # noqa: BLE001
            cost = 0.0
        for i in range(n):
            share = lambda total: total // n + (total % n if i == 0 else 0)  # noqa: E731
            _write(LlmUsageRecord(
                purpose=current_purpose() or "unlabeled", model=name, ok=True,
                project_id=ctx.get("project_id", "")[:36], job_id=ctx.get("job_id", "")[:36],
                role_id=ctx.get("role_id", "")[:64], prompt_tokens=share(prompt), completion_tokens=share(completion),
                total_tokens=share(prompt) + share(completion), cost_usd=cost / n))
            written += 1
    return written


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
