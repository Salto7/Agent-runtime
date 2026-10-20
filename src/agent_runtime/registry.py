"""Runtime id → backend."""

from __future__ import annotations

from agent_runtime.api import Runtime
from agent_runtime.docker.runtime import DockerRuntime
from agent_runtime.openshell.runtime import OpenShellRuntime
from agent_runtime.util import SharedBase


class RuntimeRegistry(SharedBase):
    """Process registry for sandbox backends."""

    def __init__(self) -> None:
        self._runtimes: dict[str, Runtime] = {}
        self._builtin_done = False

    def register(self, runtime: Runtime) -> None:
        rid = (getattr(runtime, "id", "") or "").strip()
        if not rid:
            raise ValueError("runtime.id is required")
        self._runtimes[rid] = runtime

    def get(self, runtime_id: str) -> Runtime:
        rid = (runtime_id or "").strip() or "sandbox"
        try:
            return self._runtimes[rid]
        except KeyError as exc:
            known = ", ".join(sorted(self._runtimes)) or "(none registered)"
            raise KeyError(
                f"Unknown agent runtime {rid!r}. Registered: {known}"
            ) from exc

    def ids(self) -> list[str]:
        return sorted(self._runtimes)

    def register_builtin(self) -> None:
        if self._builtin_done:
            return
        self.register(DockerRuntime())
        self.register(OpenShellRuntime())
        self._builtin_done = True
