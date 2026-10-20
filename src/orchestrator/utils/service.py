"""Process-wide default for catalog/runtime services."""

from __future__ import annotations

from agent_runtime.util import SharedBase


class SharedServiceBase(SharedBase):
    """Process-wide singleton helper (Django/orchestrator services).

    Prefer public **classmethods** that call ``cls.shared()`` internally
    (e.g. ``Session.current()``). Use ``Cls()`` + ``reset_shared()``
    for tests/DI; avoid module-level pass-through wrappers.

    Implementation lives in ``agent_runtime.util.SharedBase`` so runtime CLIs
    and orchestrator services share one singleton store.
    """
