"""OpenShell runtime. Same interface as the Docker sandbox, locked down."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_runtime.api import Runtime, RuntimeSession, RuntimeSpec, SessionInfo, TerminalHandle
from agent_runtime.openshell.cli import OpenShellCli, policy_text
from agent_runtime.openshell.session import OpenShellSession
from agent_runtime.pty import PtyTerminal
from agent_runtime.util import resource_name as make_resource_name

logger = logging.getLogger(__name__)


class OpenShellRuntime(Runtime):
    id = "openshell"

    def __init__(self) -> None:
        self._cli = OpenShellCli.shared()

    def resource_name(self, project_id: str, *, prefix: str, shared: bool) -> str:
        del shared
        return make_resource_name(project_id or "lab", prefix=prefix, suffix="-os")

    def _policy_path(self, spec: RuntimeSpec) -> Path:
        root = Path(spec.state_dir or ".").resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{spec.name}.policy.yaml"
        if not path.is_file():
            path.write_text(policy_text(), encoding="utf-8")
        return path

    def provision(self, spec: RuntimeSpec) -> RuntimeSession:
        self._cli.require_bin()
        name = (spec.name or "").strip()
        if not name:
            raise RuntimeError("openshell sandbox name is required")
        if spec.recreate:
            self.destroy(spec)
        st = self.status(spec)
        if st.get("running"):
            return self._session(spec, action="reused")
        policy = self._policy_path(spec)
        args = [
            "sandbox",
            "create",
            "--name",
            name,
            "--policy",
            str(policy),
        ]
        if spec.image:
            args.extend(["--image", spec.image])
        args.extend(["--", "sleep", "infinity"])
        created = self._cli.run(args, timeout=300)
        if not created.ok:
            err = (created.stderr or created.stdout or "").strip()
            again = self.status(spec)
            if not again.get("running"):
                raise RuntimeError(f"OpenShell sandbox create failed for {name}: {err[:500]}")
            logger.info("openshell sandbox %s already present after create: %s", name, err[:200])
            return self._session(spec, action="reused")
        return self._session(spec, action="created")

    def attach(self, spec: RuntimeSpec) -> RuntimeSession:
        self._cli.require_bin()
        st = self.status(spec)
        if not st.get("running"):
            raise RuntimeError(f"OpenShell sandbox {spec.name!r} is not running")
        return self._session(spec, action="reused")

    def _session(self, spec: RuntimeSpec, *, action: str) -> OpenShellSession:
        info = SessionInfo(
            project_id=spec.project_id,
            name=spec.name,
            mode="openshell",
            action=action,
            image=spec.image,
            workdir=spec.container_workdir or "/sandbox",
        )
        session = OpenShellSession(info, cli=self._cli)
        try:
            session.load_base_commands()
        except Exception as exc:
            logger.info("base-command probe skipped for %s: %s", spec.name, exc)
        return session

    def destroy(self, spec: RuntimeSpec) -> dict:
        name = (spec.name or "").strip()
        if not name:
            return {"ok": False, "removed": False, "error": "name required"}
        if not self._cli.available():
            return {"ok": False, "removed": False, "name": name, "error": "openshell unavailable"}
        proc = self._cli.run(["sandbox", "delete", "-n", name], timeout=120)
        if proc.ok:
            return {"ok": True, "removed": True, "name": name}
        err = (proc.stderr or proc.stdout or "").strip()
        if "not found" in err.lower() or "no such" in err.lower():
            return {"ok": True, "removed": False, "name": name, "reason": "not found"}
        return {"ok": False, "removed": False, "name": name, "error": err[:400]}

    def status(self, spec: RuntimeSpec) -> dict:
        name = spec.name
        if not self._cli.available():
            return {
                "name": name,
                "image": spec.image,
                "exists": False,
                "running": False,
                "docker": False,
                "error": "openshell CLI missing",
            }
        listed = self._cli.run(["sandbox", "list"], timeout=30)
        text = (listed.stdout or "") + (listed.stderr or "")
        running = name in text if name else False
        return {
            "name": name,
            "image": spec.image,
            "exists": running,
            "running": running,
            "docker": True,
            "error": "" if listed.ok or running else (text.strip()[:300]),
        }

    def open_terminal(self, spec: RuntimeSpec, *, cols: int, rows: int) -> TerminalHandle:
        self._cli.require_bin()
        argv = [
            self._cli.require_bin(),
            "sandbox",
            "exec",
            "-n",
            spec.name,
            "--",
            "bash",
            "-l",
        ]
        return PtyTerminal(spec.name, argv=argv, cols=cols, rows=rows)
