"""Install step ABC for tools/catalog recipes."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

class InstallStepBase(ABC):
    """One catalog install action (custom / apt / github_release / pip / git_clone)."""

    _registry: ClassVar[dict[str, type[InstallStepBase]]] = {}
    _TYPE_ALIASES: ClassVar[dict[str, str]] = {
        "command": "custom",
        "shell": "custom",
        "run": "custom",
        "bash": "custom",
        "script": "custom",
    }

    def __init_subclass__(cls, *, step_type: str = "", **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if step_type:
            InstallStepBase._registry[step_type] = cls

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> InstallStepBase | None:
        stype = str(raw.get("type") or "").strip().lower()
        if not stype and str(raw.get("command") or "").strip():
            stype = "custom"
        stype = cls._TYPE_ALIASES.get(stype, stype)
        impl = cls._registry.get(stype)
        return impl(raw) if impl else None  # type: ignore[call-arg]

    @abstractmethod
    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]: ...


