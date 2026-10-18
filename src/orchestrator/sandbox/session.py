"""Context-var sandbox session binding (SharedService).

Uses ``contextvars`` so the bound backend propagates correctly to worker
threads a host's own concurrency may use (e.g. a thread pool executor).
``threading.local()`` does not cross those pool boundaries and previously
caused ``mode='unbound'`` tool errors.

Public API is classmethods (``SandboxSession.current()``) over the process
singleton; construct ``SandboxSession()`` only for tests/DI.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar, Token

from orchestrator.sandbox.backend import SandboxBackend, SandboxInfo, UnboundSandbox
from orchestrator.utils.service import SharedService

_backend: ContextVar[SandboxBackend | None] = ContextVar(
    "sandbox_backend", default=None
)


class SandboxSession(SharedService):
    """Bind the active SandboxBackend for the current execution context."""

    def __init__(self) -> None:
        # Reset tokens stay on the binder thread; copied contexts inherit values only.
        self._tls = threading.local()

    @classmethod
    def current(cls) -> SandboxBackend:
        backend = _backend.get()
        if backend is None:
            return UnboundSandbox(
                SandboxInfo(project_id="", name="unbound", mode="unbound")
            )
        return backend

    @classmethod
    def bind(cls, backend: SandboxBackend | None) -> None:
        self = cls.shared()
        prev: Token | None = getattr(self._tls, "token", None)
        if prev is not None:
            try:
                _backend.reset(prev)
            except LookupError:
                pass
            self._tls.token = None
        self._tls.token = _backend.set(backend)

    @classmethod
    def reset(cls) -> None:
        self = cls.shared()
        token: Token | None = getattr(self._tls, "token", None)
        if token is not None:
            try:
                _backend.reset(token)
            except LookupError:
                pass
            self._tls.token = None
            return
        if _backend.get() is not None:
            _backend.set(None)
