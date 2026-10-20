"""Runtime knobs outside ``RuntimeConfig`` (memory, dreaming, authoring, proxy).

Lookup order for ``get_*``: in-process override (``set``) → environment
variable of the same name → the caller's default. A host that keeps operator
settings in its own store calls :func:`set_resolver` once and the rest of the
library reads through it unchanged.
"""

from __future__ import annotations

import os
from typing import Callable

_overrides: dict[str, str] = {}
_resolver: Callable[[str], str | None] | None = None
_TRUE = {"1", "true", "yes", "on"}


def set_resolver(resolver: Callable[[str], str | None] | None) -> None:
    """Install ``resolver(key) -> str | None`` consulted before the environment."""
    global _resolver
    _resolver = resolver


def set(key: str, value: object | None) -> None:  # noqa: A001 — mirrors a settings API
    """Override ``key`` for this process (``None`` removes the override)."""
    if value is None:
        _overrides.pop(key, None)
    else:
        _overrides[key] = str(value)


def reset() -> None:
    _overrides.clear()


def _raw(key: str) -> str | None:
    if key in _overrides:
        return _overrides[key]
    if _resolver is not None:
        try:
            value = _resolver(key)
        except Exception:  # noqa: BLE001 — a broken host resolver must not break the runtime
            value = None
        if value is not None:
            return str(value)
    env = os.environ.get(key)
    return env if env is not None and env.strip() != "" else None


def get_str(key: str, default: str = "") -> str:
    raw = _raw(key)
    return default if raw is None else raw.strip()


def get_int(key: str, default: int = 0) -> int:
    raw = _raw(key)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        pass
    try:
        return int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_raw(key) if _raw(key) is not None else default)
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    raw = _raw(key)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def cognee_enabled() -> bool:
    """Cognee memory is on when ``COGNEE_ENABLED`` says so; unset, it is on only if a
    server is configured (``COGNEE_URL`` or ``COGNEE_HOST``) — no server, no failed calls."""
    explicit = _raw("COGNEE_ENABLED")
    if explicit is not None:
        return explicit.strip().lower() in _TRUE
    return bool(get_str("COGNEE_URL") or get_str("COGNEE_HOST"))
