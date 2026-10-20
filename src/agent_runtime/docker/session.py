"""Run commands inside a Docker container. This is the sandbox exec path."""

from __future__ import annotations

import socket
import threading
import time

from docker.errors import DockerException
from docker.utils.socket import STDERR, frames_iter

from agent_runtime.api import ExecResult, RuntimeSession, SessionInfo
from agent_runtime.docker.engine import DockerEngine
from agent_runtime.process import ExecTransport
from agent_runtime.util import forward_env_items

# Same code ``subprocess`` timeouts map to in ``HostProcessRunner``.
_TIMEOUT_CODE = 124


class DockerSession(RuntimeSession):
    def __init__(self, info: SessionInfo, *, engine: DockerEngine | None = None) -> None:
        super().__init__(info)
        self._engine = engine or DockerEngine.shared()

    def _environment(self, env: dict[str, str] | None) -> dict[str, str]:
        out = {
            "DEBIAN_FRONTEND": "noninteractive",
            "ORCHESTRATOR_WORKSPACE": self.workdir(),
            "SKILLS_DIR": "/skills",
            "SKILLS_IMPORTED_DIR": "/skills",
            "TOOLS_CATALOG_DIR": "/tools/catalog",
            "PROJECT_WORKSPACES_DIR": "/workspace",
            # Include sbin paths — Kali (and Debian) put many admin CLIs there.
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "/skills/helpers",
        }
        out.update(
            forward_env_items(
                self.env_get,
                skip={"ORCHESTRATOR_WORKSPACE", "ORCHESTRATOR_SANDBOX_WORKDIR"},
            )
        )
        for key, val in (env or {}).items():
            if key and val is not None:
                out[key] = str(val)
        return out

    def exec(
        self,
        cmd: list[str] | str,
        *,
        timeout: float | None = 300,
        shell: bool = False,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> ExecResult:
        api = self._engine.api
        try:
            exec_id = api.exec_create(
                self.info.name,
                ExecTransport.command(cmd, shell=shell),
                stdin=stdin is not None,
                workdir=cwd or self.workdir(),
                environment=self._environment(env),
            )["Id"]
            stream = api.exec_start(exec_id, socket=True)
        except DockerException as exc:
            return ExecResult(126, "", str(getattr(exc, "explanation", None) or exc))

        raw = getattr(stream, "_sock", stream)
        out: list[bytes] = []
        err: list[bytes] = []
        failure: list[str] = []

        def pump() -> None:
            try:
                for stream_id, data in frames_iter(stream, tty=False):
                    (err if stream_id == STDERR else out).append(data)
            except OSError as exc:
                failure.append(str(exc))

        def feed() -> None:
            try:
                raw.sendall(stdin.encode())
                raw.shutdown(socket.SHUT_WR)
            except OSError as exc:
                failure.append(str(exc))

        # Read while writing: a command that echoes a large stdin would otherwise fill its output pipe and
        # block our sendall forever (and the timeout below would never be reached).
        reader = threading.Thread(target=pump, name=f"exec-{exec_id[:12]}", daemon=True)
        reader.start()
        if stdin is not None:
            threading.Thread(target=feed, name=f"stdin-{exec_id[:12]}", daemon=True).start()
        reader.join(timeout)
        timed_out = reader.is_alive()
        if timed_out:
            # Unblocks the reader; the command keeps running, as with a killed ``docker exec``.
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            reader.join(5)
        stream.close()

        stdout = b"".join(out).decode("utf-8", errors="replace")
        stderr = b"".join(err).decode("utf-8", errors="replace")
        if timed_out:
            return ExecResult(_TIMEOUT_CODE, stdout, stderr or f"timed out after {timeout}s")
        if failure:
            stderr = f"{stderr}\nexec stream error: {failure[0]}".lstrip()
        code = self._exit_code(exec_id)
        if code in (126, 127) and not stderr and stdout.startswith("OCI runtime"):
            # The engine reports start failures on the stdout frame; the CLI printed them on stderr.
            stdout, stderr = "", stdout.strip()
        return ExecResult(code, stdout, stderr)

    def _exit_code(self, exec_id: str) -> int:
        # The stream can close a moment before the engine records the exit code.
        for _ in range(50):
            info = self._engine.api.exec_inspect(exec_id)
            if not info.get("Running") and info.get("ExitCode") is not None:
                return int(info["ExitCode"])
            time.sleep(0.02)
        return int(self._engine.api.exec_inspect(exec_id).get("ExitCode") or 0)

