"""Register capability callables by name for ``allowed-tools`` filtering.

Called capabilities (not “tools”) to avoid confusion with ``tools/catalog``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

_REGISTERED = False


class CapabilityGroup(str, Enum):
    SANDBOX = "sandbox"
    ENGAGEMENT = "engagement"


@dataclass(frozen=True)
class RegisteredCapability:
    group: CapabilityGroup
    handler: Callable[..., Any]
    tags: frozenset[str] = field(default_factory=frozenset)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, RegisteredCapability] = {}

    def register(
        self,
        group: CapabilityGroup,
        handler: Callable[..., Any],
        *,
        tags: Iterable[str] = (),
        name: str = "",
    ) -> Callable[..., Any]:
        key = (name or getattr(handler, "__name__", "") or "").strip()
        if not key:
            raise ValueError("Capability must have a name")
        if key in self._by_name:
            raise ValueError(f"Duplicate capability registration: {key!r}")
        self._by_name[key] = RegisteredCapability(
            group=group,
            handler=handler,
            tags=frozenset(str(t).strip() for t in tags if str(t).strip()),
        )
        return handler

    def get(self, name: str) -> RegisteredCapability | None:
        return self._by_name.get(name)

    def names(
        self,
        *,
        groups: Iterable[CapabilityGroup] | None = None,
        exclude_tags: Iterable[str] = (),
        include: Iterable[str] | None = None,
        exclude: Iterable[str] = (),
    ) -> list[str]:
        wanted = set(groups) if groups is not None else None
        skip_tags = {str(t).strip() for t in exclude_tags if str(t).strip()}
        allow = {str(n).strip() for n in include} if include is not None else None
        deny = {str(n).strip() for n in exclude if str(n).strip()}
        out: list[str] = []
        for name, entry in self._by_name.items():
            if name in deny:
                continue
            if allow is not None and name not in allow:
                continue
            if wanted is not None and entry.group not in wanted:
                continue
            if entry.tags & skip_tags:
                continue
            out.append(name)
        return out


REGISTRY = CapabilityRegistry()

_GROUP_ORDER = (
    CapabilityGroup.SANDBOX,
    CapabilityGroup.ENGAGEMENT,
)
_SKIP_AUTHORING_TAGS = frozenset({"watchdog", "periodic", "subagent"})
# catalog_wrapper: sandbox status + provision_cli only.
_CATALOG_SANDBOX = frozenset({"sandbox_status", "provision_cli"})


def ensure_registered() -> CapabilityRegistry:
    """Import capability tools once so ``@capability`` side-effects populate REGISTRY."""
    global _REGISTERED
    if not _REGISTERED:
        # circular: capabilities.tools → CapabilityGroup / capability from this module
        import orchestrator.capabilities.tools  # noqa: F401

        _REGISTERED = True
    return REGISTRY


def default_allowed_tools(*, mode: str = "catalog_wrapper") -> str:
    """``allowed-tools`` string from the live registry for role authoring.

    - ``catalog_wrapper``: sandbox status/provision + engagement;
      skip watchdog/periodic/subagent tags.
    - ``capability``: same as catalog_wrapper plus the Claude ``bash`` shell.
    """
    reg = ensure_registered()
    names: list[str] = []
    for group in _GROUP_ORDER:
        for name in sorted(reg.names(groups=(group,), exclude_tags=_SKIP_AUTHORING_TAGS)):
            entry = reg.get(name)
            if entry is None:
                continue
            if entry.group == CapabilityGroup.SANDBOX:
                if name in _CATALOG_SANDBOX or mode == "capability" and "shell" in entry.tags:
                    names.append(name)
                continue
            names.append(name)
    return " ".join(names)


def capability(group: CapabilityGroup, *, tags: Iterable[str] = ()):
    """Register a plain capability callable under ``group``."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return REGISTRY.register(group, fn, tags=tags)

    return decorator
