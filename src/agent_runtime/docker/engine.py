"""Docker Engine client (docker SDK). Lifecycle only — command execution goes through DockerSession."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from agent_runtime.api import ExecResult
from agent_runtime.util import SharedBase

logger = logging.getLogger(__name__)

_API_TIMEOUT = 600


def _error(exc: Exception) -> ExecResult:
    detail = getattr(exc, "explanation", None) or str(exc)
    return ExecResult(1, "", str(detail))


def _filters(items: tuple[str, ...]) -> dict[str, list[str]]:
    """``"label=k=v"`` CLI-style filter strings → SDK ``filters`` dict."""
    out: dict[str, list[str]] = {}
    for item in items:
        key, _, value = str(item).partition("=")
        if key and value:
            out.setdefault(key, []).append(value)
    return out


class DockerEngine(SharedBase):
    """Process-shared ``docker.DockerClient`` honoring ``DOCKER_HOST``."""

    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None
        self._self_mounts_cache: list[dict[str, Any]] | None = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env(timeout=_API_TIMEOUT)
        return self._client

    @property
    def api(self) -> docker.APIClient:
        return self.client.api

    def daemon_ok(self) -> tuple[bool, str]:
        try:
            self.client.ping()
        except DockerException as exc:
            self._client = None
            return False, str(exc) or "docker daemon unreachable"
        return True, ""

    def available(self) -> bool:
        return self.daemon_ok()[0]

    def require_daemon(self) -> None:
        ok, err = self.daemon_ok()
        if not ok:
            raise RuntimeError(
                "Docker daemon unreachable — ensure DOCKER_HOST or "
                f"/var/run/docker.sock is reachable. Detail: {(err or '')[:400]}"
            )

    def docker_bin(self) -> str:
        """Docker CLI path, used only for interactive ``docker exec -it`` terminals."""
        path = shutil.which("docker")
        if not path:
            raise RuntimeError("docker CLI missing — required for sandbox terminals")
        return path

    def container(self, ref: str) -> Container | None:
        try:
            return self.client.containers.get(ref)
        except NotFound:
            return None

    def inspect_running(self, name: str) -> tuple[bool, bool]:
        try:
            box = self.container(name)
        except DockerException:
            return False, False
        if box is None:
            return False, False
        return True, box.status == "running"

    def image_id(self, ref: str) -> str:
        try:
            return self.client.images.get(ref).id or ""
        except (ImageNotFound, DockerException):
            return ""

    def labels(self, ref: str) -> dict[str, str]:
        try:
            box = self.container(ref)
        except DockerException:
            return {}
        if box is None:
            return {}
        return {str(k): str(v) for k, v in (box.labels or {}).items() if str(k).strip()}

    def networks(self, ref: str) -> dict[str, Any]:
        try:
            box = self.container(ref)
        except DockerException:
            return {}
        if box is None:
            return {}
        nets = (box.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        return nets if isinstance(nets, dict) else {}

    def start(self, name: str) -> ExecResult:
        try:
            self.client.containers.get(name).start()
        except DockerException as exc:
            return _error(exc)
        return ExecResult(0)

    def rm_force(self, ref: str) -> ExecResult:
        try:
            self.client.containers.get(ref).remove(force=True)
        except DockerException as exc:
            return _error(exc)
        return ExecResult(0)

    def pull(self, image: str) -> ExecResult:
        try:
            self.client.images.pull(image)
        except DockerException as exc:
            return _error(exc)
        return ExecResult(0)

    def ps_ids(self, *filters: str, all_containers: bool = True) -> list[str]:
        try:
            listed = self.client.containers.list(all=all_containers, filters=_filters(filters))
        except DockerException:
            return []
        return [box.id for box in listed]

    def run_detached(
        self,
        *,
        name: str,
        image: str,
        workdir: str = "/tmp",
        labels: dict[str, str] | None = None,
        volumes: list[str] | None = None,
        network: str = "",
        network_aliases: list[str] | None = None,
        env: dict[str, str] | None = None,
        ports: dict[str, int] | None = None,
        extra_hosts: dict[str, str] | None = None,
        restart_policy: str = "",
        command: list[str] | None = None,
        shm_size: str = "",
    ) -> ExecResult:
        kwargs: dict[str, Any] = {
            "name": name,
            "detach": True,
            "working_dir": workdir,
            "labels": dict(labels or {}),
            "environment": dict(env or {}),
            "command": command or ["sleep", "infinity"],
        }
        if volumes:
            kwargs["volumes"] = list(volumes)
        if network:
            kwargs["network"] = network
            if network_aliases:
                kwargs["networking_config"] = {
                    network: self.api.create_endpoint_config(aliases=list(network_aliases))
                }
        if ports:
            kwargs["ports"] = dict(ports)
        if extra_hosts:
            kwargs["extra_hosts"] = dict(extra_hosts)
        if restart_policy:
            kwargs["restart_policy"] = {"Name": restart_policy}
        if shm_size:
            kwargs["shm_size"] = shm_size
        try:
            box = self.client.containers.run(image, **kwargs)
        except DockerException as exc:
            return _error(exc)
        return ExecResult(0, box.id or "")

    def ensure_running(
        self,
        name: str,
        *,
        image: str,
        workdir: str = "/tmp",
        labels: dict[str, str] | None = None,
        volumes: list[str] | None = None,
        network: str = "",
        env: dict[str, str] | None = None,
        extra_hosts: dict[str, str] | None = None,
        command: list[str] | None = None,
        pull_image: bool = False,
        shm_size: str = "",
    ) -> str:
        exists, running = self.inspect_running(name)
        if exists and running:
            return "reused"
        if exists and not running:
            start = self.start(name)
            if not start.ok:
                raise RuntimeError(f"Failed to start container {name}: {start.stderr}")
            return "started"

        if pull_image:
            pull = self.pull(image)
            if not pull.ok:
                logger.warning("image pull soft-failed for %s: %s", image, pull.stderr[:200])

        create = self.run_detached(
            name=name,
            image=image,
            workdir=workdir,
            labels=labels,
            volumes=volumes,
            network=network,
            env=env,
            extra_hosts=extra_hosts,
            command=command,
            shm_size=shm_size,
        )
        if create.ok:
            return "created"

        exists2, running2 = self.inspect_running(name)
        if exists2:
            if not running2:
                self.start(name)
            logger.info("container %s already existed after create race; reusing", name)
            return "reused"
        raise RuntimeError(f"Failed to create container {name}: {create.stderr}")

    def host_bind_path(self, path: Path) -> str:
        resolved = path.resolve()
        path_s = str(resolved)
        for mount in self.self_mounts():
            dest = str(mount.get("Destination") or "")
            src = str(mount.get("Source") or "")
            if not dest or not src:
                continue
            if path_s == dest or path_s.startswith(dest.rstrip("/") + "/"):
                rel = path_s[len(dest) :].lstrip("/")
                return str(Path(src) / rel) if rel else src
        return path_s

    def self_container_id(self) -> str:
        """Hostname of this process when running inside Docker; else empty."""
        if not Path("/.dockerenv").exists():
            return ""
        try:
            return Path("/etc/hostname").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def self_mounts(self) -> list[dict[str, Any]]:
        if self._self_mounts_cache is not None:
            return self._self_mounts_cache
        cid = self.self_container_id()
        mounts: list[dict[str, Any]] = []
        if cid:
            try:
                box = self.container(cid)
            except DockerException:
                box = None
            if box is not None:
                found = box.attrs.get("Mounts") or []
                mounts = found if isinstance(found, list) else []
        self._self_mounts_cache = mounts
        return mounts


__all__ = ["DockerEngine"]
