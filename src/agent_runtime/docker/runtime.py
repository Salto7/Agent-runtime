"""Docker runtime: provision, exec, destroy, terminal."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from agent_runtime.api import Runtime, RuntimeSession, RuntimeSpec, SessionInfo, TerminalHandle
from agent_runtime.docker.engine import DockerEngine
from agent_runtime.docker.session import DockerSession
from agent_runtime.pty import PtyTerminal
from agent_runtime.util import resource_name as make_resource_name

logger = logging.getLogger(__name__)

# Chromium (Playwright) crashes on Docker's default 64MB /dev/shm.
_SANDBOX_SHM = "1gb"


class DockerRuntime(Runtime):
    id = "sandbox"

    @staticmethod
    def mode_for(spec: RuntimeSpec) -> str:
        if spec.role == "learn-lab":
            return "learn-lab"
        if spec.role == "shared":
            return "shared"
        return "docker"

    def resource_name(self, project_id: str, *, prefix: str, shared: bool) -> str:
        return make_resource_name(project_id, prefix=prefix, shared=shared)

    def _engine(self) -> DockerEngine:
        return DockerEngine.shared()

    def _volumes(self, spec: RuntimeSpec) -> list[str]:
        """Bind specs (``src:dst[:ro]``) as the Docker SDK ``volumes`` list."""
        bind = self._engine().host_bind_path
        out: list[str] = []
        if spec.workspace_host:
            out.append(f"{bind(Path(spec.workspace_host))}:/workspace")
        if spec.skills_host:
            out.append(f"{bind(Path(spec.skills_host))}:/skills:ro")
        if spec.skills_imported_host:
            out.append(f"{bind(Path(spec.skills_imported_host))}:/skills-imported:ro")
        for host, container in spec.extra_ro_mounts or []:
            if not host or not container or not container.startswith("/"):
                continue
            out.append(f"{bind(Path(host))}:{container}:ro")
        if spec.tools_host:
            tools = Path(spec.tools_host)
            tools_root = tools.parent if tools.name == "catalog" else tools
            out.append(f"{bind(tools_root)}:/tools:ro")
        if spec.socket_volume:
            out.append(f"{spec.socket_volume}:/tmp/orchestrator")
        elif spec.socket_dir_host:
            parent = Path(spec.socket_dir_host)
            out.append(f"{bind(parent)}:{parent}")
        return out

    _VOLUME_PATH = re.compile(r"^/var/lib/docker/volumes/([^/:]+)/_data(?=/|:|$)")

    @classmethod
    def _canonical_volume(cls, volume: str) -> str:
        """``/var/lib/docker/volumes/<n>/_data/x:/c`` ≡ ``<n>/x:/c`` (named volume).

        The web process and the worker can see the same Docker volume as a host
        path or as a name; without this they fingerprint a sandbox differently
        and keep recreating each other's container.
        """
        return cls._VOLUME_PATH.sub(r"\1", volume)

    def _runtime_fingerprint(self, spec: RuntimeSpec, volumes: list[str]) -> str:
        """Identify the image/configuration a reusable sandbox was created with."""
        image_ref = (spec.image or "").strip()
        image = self._engine().image_id(image_ref)
        payload = {
            "image": image or image_ref,
            "workdir": spec.container_workdir or "/workspace",
            "volumes": sorted(self._canonical_volume(v) for v in volumes),
            "network": (spec.network or "").strip(),
            "env": sorted((str(k), str(v)) for k, v in (spec.env or {}).items()),
            "extra_hosts": sorted((spec.extra_hosts or {}).items()),
            "shm_size": _SANDBOX_SHM,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]

    def provision(self, spec: RuntimeSpec) -> RuntimeSession:
        cli = self._engine()
        cli.require_daemon()
        name = (spec.name or "").strip()
        if not name:
            raise RuntimeError("sandbox name is required")
        image = (spec.image or "").strip()
        if not image:
            raise RuntimeError("sandbox image is required")
        volumes = self._volumes(spec)
        # Services on the host machine are reachable from the sandbox.
        # The sandbox stays off the Compose network.
        extra_hosts = {"host.docker.internal": "host-gateway", **(spec.extra_hosts or {})}
        spec = RuntimeSpec(**{**spec.__dict__, "extra_hosts": extra_hosts})
        fingerprint = self._runtime_fingerprint(spec, volumes)
        labels = dict(spec.labels or {})
        labels["orchestrator.runtime_fingerprint"] = fingerprint
        if spec.recreate:
            cli.rm_force(name)
        else:
            exists, _ = cli.inspect_running(name)
            if exists and cli.labels(name).get("orchestrator.runtime_fingerprint") != fingerprint:
                removed = cli.rm_force(name)
                if not removed.ok:
                    detail = (removed.stderr or removed.stdout or "").strip()
                    raise RuntimeError(f"Failed to recreate sandbox {name}: {detail}")
        action = cli.ensure_running(
            name,
            image=image,
            workdir=spec.container_workdir or "/workspace",
            labels={key: value for key, value in labels.items() if key},
            volumes=volumes or None,
            network=(spec.network or "").strip(),
            env=dict(spec.env or {}),
            extra_hosts=dict(spec.extra_hosts or {}),
            pull_image=spec.pull_image,
            shm_size=_SANDBOX_SHM,
        )
        return self._session(spec, action=action)

    def attach(self, spec: RuntimeSpec) -> RuntimeSession:
        return self._session(spec, action="reused")

    def _session(self, spec: RuntimeSpec, *, action: str) -> DockerSession:
        info = SessionInfo(
            project_id=spec.project_id,
            name=spec.name,
            mode=self.mode_for(spec),
            action=action,
            image=spec.image,
            workdir=spec.container_workdir or "/workspace",
        )
        session = DockerSession(info, engine=self._engine())
        try:
            session.load_base_commands()
        except Exception as exc:
            logger.info("base-command probe skipped for %s: %s", spec.name, exc)
        return session

    def destroy(self, spec: RuntimeSpec) -> dict:
        name = (spec.name or "").strip()
        if not name:
            return {"ok": False, "removed": False, "error": "name required"}
        if spec.role == "shared":
            return {"ok": True, "removed": False, "name": name, "reason": "shared"}
        cli = self._engine()
        if not cli.available():
            return {"ok": False, "removed": False, "name": name, "error": "docker unavailable"}
        proc = cli.rm_force(name)
        if proc.ok:
            return {"ok": True, "removed": True, "name": name}
        err = (proc.stderr or proc.stdout or "").strip()
        if "No such container" in err:
            return {"ok": True, "removed": False, "name": name, "reason": "not found"}
        return {"ok": False, "removed": False, "name": name, "error": err}

    def status(self, spec: RuntimeSpec) -> dict:
        name = spec.name
        image = spec.image
        cli = self._engine()
        ok, err = cli.daemon_ok()
        if not ok:
            return {
                "name": name,
                "image": image,
                "exists": False,
                "running": False,
                "docker": False,
                "error": (err or "")[:300],
            }
        exists, running = cli.inspect_running(name) if name else (False, False)
        return {
            "name": name,
            "image": image,
            "exists": exists,
            "running": running,
            "docker": True,
            "error": "",
        }

    def find(self, label: str) -> list[str]:
        cli = self._engine()
        if not label or not cli.available():
            return []
        return cli.ps_ids(label)

    def open_terminal(self, spec: RuntimeSpec, *, cols: int, rows: int) -> TerminalHandle:
        workdir = spec.container_workdir or "/workspace"
        argv = [
            self._engine().docker_bin(),
            "exec",
            "-i",
            "-t",
            "-w",
            workdir,
            "-e",
            "TERM=xterm-256color",
            "-e",
            "LANG=C.UTF-8",
            spec.name,
            "/bin/bash",
            "-l",
        ]
        return PtyTerminal(spec.name, argv=argv, cols=cols, rows=rows)
