"""Shared helpers for agent_runtime backends (no imports from api)."""

from __future__ import annotations

from typing import Any, ClassVar, TypeVar

T = TypeVar("T", bound="SharedBase")

# Job env keys forwarded into sandboxed execs (Docker + OpenShell).
FORWARD_ENV = (
    "ORCHESTRATOR_JOB_ID",
    "ORCHESTRATOR_STREAM_SOCKET",
    "ORCHESTRATOR_IN_SCOPE",
    "ORCHESTRATOR_EXCLUSIONS",
    "ORCHESTRATOR_SEED",
    "ORCHESTRATOR_PROJECT_ID",
    "ORCHESTRATOR_JOB_BRIEF",
    "ORCHESTRATOR_WORKSPACE",
    "ORCHESTRATOR_SANDBOX_WORKDIR",
)


def forward_env_items(
    lookup, *, skip: frozenset[str] | set[str] | None = None
) -> list[tuple[str, str]]:
    """Non-empty ``FORWARD_ENV`` pairs via ``lookup(key) -> str``."""
    ignore = skip or ()
    out: list[tuple[str, str]] = []
    for key in FORWARD_ENV:
        if key in ignore:
            continue
        val = lookup(key)
        if val:
            out.append((key, val))
    return out



class SharedBase:
    """Process-singleton mixin (mirrors orchestrator SharedServiceBase, no Django)."""

    _shared: ClassVar[dict[type, Any]] = {}

    @classmethod
    def shared(cls: type[T]) -> T:
        if cls not in cls._shared:
            cls._shared[cls] = cls()
        return cls._shared[cls]  # type: ignore[return-value]

    @classmethod
    def reset_shared(cls) -> None:
        cls._shared.pop(cls, None)


def sanitize_name(value: str) -> str:
    return "".join(
        ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value).strip()
    ).strip("-")


def resource_name(
    project_id: str,
    *,
    prefix: str,
    shared: bool = False,
    suffix: str = "",
) -> str:
    """Docker/OpenShell resource id (≤63 chars)."""
    base = (prefix or "orchestrator-project").strip()
    if suffix:
        base = f"{base}{suffix}"
    if shared or not (project_id or "").strip():
        return f"{base}-shared"
    safe = sanitize_name(project_id)
    budget = max(8, 63 - len(base) - 1)
    return f"{base}-{safe[:budget]}"
