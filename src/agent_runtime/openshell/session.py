"""Run commands inside an OpenShell sandbox."""

from __future__ import annotations

from agent_runtime.api import ExecResult, RuntimeSession, SessionInfo
from agent_runtime.openshell.cli import OpenShellCli
from agent_runtime.process import ExecTransport, HostProcessRunner
from agent_runtime.util import forward_env_items


class OpenShellSession(RuntimeSession):
    def __init__(self, info: SessionInfo, *, cli: OpenShellCli | None = None) -> None:
        super().__init__(info)
        self._cli = cli or OpenShellCli.shared()

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
        work = cwd or self.workdir()
        workspace = self.workdir()
        remote = ExecTransport.command_in(cmd, cwd=work, shell=shell)
        env_prefix = [f"{k}={v}" for k, v in forward_env_items(self.env_get)]
        # Match DockerSession so skill scripts see the same mount layout.
        env_prefix.extend(
            [
                f"ORCHESTRATOR_WORKSPACE={workspace}",
                "SKILLS_DIR=/skills",
                "SKILLS_IMPORTED_DIR=/skills",
                "TOOLS_CATALOG_DIR=/tools/catalog",
                "PROJECT_WORKSPACES_DIR=/workspace",
                "PYTHONPATH=/skills/helpers",
            ]
        )
        for key, val in (env or {}).items():
            if key and val is not None:
                env_prefix.append(f"{key}={val}")
        if env_prefix:
            remote = ["env", *env_prefix, *remote]
        argv = [
            self._cli.require_bin(),
            "sandbox",
            "exec",
            "-n",
            self.info.name,
            "--",
            *remote,
        ]
        return HostProcessRunner.run(argv, timeout=timeout, shell=False, stdin=stdin)
