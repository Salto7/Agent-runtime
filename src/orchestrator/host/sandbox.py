"""Project runtime lifecycle with one dedicated environment per project (no Django).

Chooses the runtime backend (``sandbox`` = Docker, ``openshell``), provisions or
attaches the project's container, and binds it as the process ``Session``. It
does not run commands itself.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from agent_runtime.api import Runtime, RuntimeSpec, Session, SessionInfo
from agent_runtime.registry import RuntimeRegistry
from orchestrator import settings
from orchestrator.config import get_config
from orchestrator.utils.job_env import JobEnv
from orchestrator.utils.paths import safe_workspace_key
from orchestrator.utils.service import SharedServiceBase

logger = logging.getLogger(__name__)


SANDBOX = "sandbox"
OPENSHELL = "openshell"


def resolve_runtime(raw: str | None) -> str:
    """``sandbox`` (Docker, default) or ``openshell``; anything else falls back to Docker."""
    value = (raw or "").strip().lower()
    return value if value in {SANDBOX, OPENSHELL} else SANDBOX


def runtime_state_dir() -> Path:
    """Docker/OpenShell metadata in the reserved platform workspace."""
    return Path(get_config().platform_workspace_dir).resolve() / "runtime"


def bind_runtime_session(session: Any, *, default_workdir: str = "/workspace") -> None:
    """Set workdir env, JobEnv lookup, and ``Session.bind``."""

    work = (session.info.workdir or default_workdir).strip() or default_workdir
    os.environ["ORCHESTRATOR_SANDBOX_WORKDIR"] = work
    if JobEnv.current():
        JobEnv.bind({**JobEnv.current(), "ORCHESTRATOR_SANDBOX_WORKDIR": work})
    else:
        JobEnv.bind({"ORCHESTRATOR_SANDBOX_WORKDIR": work})
    session.set_env_lookup(JobEnv.get)
    Session.bind(session)


#: Per-project runtime choice (host-supplied); unknown projects use ``SANDBOX_RUNTIME``.
_project_runtimes: dict[str, str] = {}


def set_project_runtime(project_id: str, runtime_id: str) -> None:
    _project_runtimes[str(project_id or "").strip()] = resolve_runtime(runtime_id)


def runtime_id_for(project_id: str) -> str:
    pid = str(project_id or "").strip()
    return _project_runtimes.get(pid) or resolve_runtime(settings.get_str("SANDBOX_RUNTIME", SANDBOX))


class ProjectSandbox(SharedServiceBase):
    """Choose the project's runtime and bind a session. Does not run commands."""


    def per_project(self) -> bool:
        return True

    def prefix(self) -> str:
        return (get_config().sandbox_prefix or "orchestrator-project").strip()

    def image_for(self, runtime_id: str) -> str:
        sandbox = (get_config().sandbox_image or "").strip()
        if runtime_id == OPENSHELL:
            return settings.get_str("OPENSHELL_IMAGE") or sandbox
        return sandbox

    def runtime_for(self, project_id: str) -> Runtime:
        registry = RuntimeRegistry.shared()
        registry.register_builtin()
        return registry.get(runtime_id_for(project_id))

    @classmethod
    def container_name(cls, project_id: str) -> str:
        self = cls.shared()
        pid = str(project_id or "").strip()
        runtime = self.runtime_for(pid)
        return runtime.resource_name(
            pid,
            prefix=self.prefix(),
            shared=False,
        )

    @classmethod
    def provision(
        cls,
        project_id: str,
        *,
        bind: bool = True,
        warm_mcp: bool = True,
        socket_dir: str | Path = "",
    ) -> SessionInfo:
        """Create or attach the project runtime.

        Job paths bind the session and warm the MCP supervisor. The operator
        terminal passes ``bind=False, warm_mcp=False``: it only needs the
        container up, and the MCP warm-up can take minutes on a cold image.
        """
        runtime, spec = cls.spec_for(str(project_id or "").strip(), socket_dir=socket_dir)
        session = runtime.provision(spec)
        if bind:
            bind_runtime_session(session, default_workdir="/workspace")
        mode = (session.info.mode or "").strip().lower()
        if warm_mcp and mode and mode != "openshell":
            try:
                from orchestrator.mcp.provision import ensure_mcp_runtime

                warm = ensure_mcp_runtime(session)
                if warm:
                    logger.warning("mcp supervisor warm-up: %s", warm[:300])
            except Exception as exc:
                logger.warning("mcp supervisor warm-up skipped: %s", exc)
        return session.info

    @classmethod
    def remove(
        cls,
        project_id: str,
        *,
        runtime_id: str = "",
    ) -> dict[str, Any]:
        self = cls.shared()
        pid = str(project_id or "").strip()
        if not pid:
            return {"action": "skipped", "reason": "no project_id"}

        registry = RuntimeRegistry.shared()
        registry.register_builtin()
        selected = (
            resolve_runtime(runtime_id)
            if (runtime_id or "").strip()
            else runtime_id_for(pid)
        )
        runtime = registry.get(selected)
        dedicated = runtime.resource_name(pid, prefix=self.prefix(), shared=False)
        removed: list[str] = []
        errors: list[str] = []

        def _rm(target: str, *, role: str) -> None:
            result = runtime.destroy(
                RuntimeSpec(
                    project_id=pid,
                    name=target,
                    role=role,
                    image=self.image_for(runtime.id),
                )
            )
            if result.get("removed"):
                removed.append(target)
                return
            err = str(result.get("error") or "")
            if err:
                errors.append(f"{target}: {err}")

        try:
            _rm(dedicated, role="project")
        except Exception as exc:
            errors.append(f"{dedicated}: {exc}")

        try:
            for cid in runtime.find(f"label=orchestrator.project_id={pid}"):
                if cid in removed or cid == dedicated:
                    continue
                try:
                    _rm(cid, role="project")
                except Exception as exc:
                    errors.append(f"{cid}: {exc}")
        except Exception as exc:
            errors.append(str(exc))

        policy = runtime_state_dir() / f"{dedicated}.policy.yaml"
        try:
            policy.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{policy}: {exc}")

        Session.reset()
        if removed:
            return {
                "action": "removed",
                "name": dedicated,
                "removed": removed,
                "errors": errors,
            }
        if errors:
            return {"action": "error", "name": dedicated, "errors": errors}
        return {"action": "missing", "name": dedicated}

    @classmethod
    def spec_for(
        cls, project_id: str, *, socket_dir: str | Path = ""
    ) -> tuple[Runtime, RuntimeSpec]:
        """Runtime and spec for the project's dedicated environment.

        ``socket_dir`` is the host directory holding this run's stream/RPC unix
        sockets; it is mounted at the same path so in-sandbox skills can reach them.
        """
        self = cls.shared()
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ValueError("project_id is required for an isolated sandbox")
        cfg = get_config()
        runtime = self.runtime_for(project_id)
        ws = Path(cfg.workspaces_dir).resolve() / safe_workspace_key(project_id)
        ws.mkdir(parents=True, exist_ok=True)
        # Catalog packs are materialized from the catalog into the workspace.
        # Only the skill runtime helpers are mounted.
        helper_pack = Path(cfg.skills_dir).resolve() / "helpers"
        extra_mounts = [(str(helper_pack), "/skills/helpers")] if helper_pack.is_dir() else []
        volume = settings.get_str("SANDBOX_SOCKETS_VOLUME")
        sock_dir = str(socket_dir or "")
        spec = RuntimeSpec(
            project_id=project_id,
            name=runtime.resource_name(project_id, prefix=self.prefix(), shared=False),
            image=self.image_for(runtime.id),
            role="project",
            workspace_host=str(ws),
            extra_ro_mounts=extra_mounts,
            tools_host=str(Path(cfg.tools_catalog_dir).resolve()),
            socket_dir_host="" if volume else sock_dir,
            socket_volume=volume,
            container_workdir="/sandbox" if runtime.id == OPENSHELL else "/workspace",
            labels={"orchestrator.role": "project-sandbox", "orchestrator.project_id": project_id},
            state_dir=str(runtime_state_dir()),
        )
        return runtime, spec
