"""Throwaway Docker lab for Toolsmith install tests.

Uses the project sandbox image so packages match real sandboxes.
The container is separate from any project sandbox and is recreated clean
before each install test.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml

from agent_runtime.api import RuntimeSession, RuntimeSpec, Session, SessionInfo
from agent_runtime.docker.engine import DockerEngine
from agent_runtime.registry import RuntimeRegistry
from orchestrator import settings
from orchestrator.config import get_config
from orchestrator.host.sandbox import bind_runtime_session, runtime_state_dir
from orchestrator.llm.proxy import LlmProxy
from orchestrator.tools.catalog import ToolCatalog
from orchestrator.tools.catalog.catalog import CatalogProvisioner
from orchestrator.utils.service import SharedServiceBase
from orchestrator.utils.strings import is_kebab

_PURPOSE_TEST = "test"
_PURPOSE_AUTHOR = "author"


class LearnLab(SharedServiceBase):
    """Two throwaway containers: a clean install-test lab and an authoring lab.

    The test lab (``<name>-test``) is what the Toolsmith switch shows. Each
    install test recreates it so leftover packages cannot satisfy verify.
    The authoring lab (``<name>``) keeps OpenCode installed across drafts.
    """

    def image(self) -> str:
        return settings.get_str("LEARN_LAB_IMAGE") or get_config().sandbox_image

    def container_name(self) -> str:
        return settings.get_str("LEARN_LAB_CONTAINER", "orchestrator-learn-lab")

    def test_container_name(self) -> str:
        return f"{self.container_name()}-test"

    def staging_root(self) -> Path:
        root = Path(get_config().platform_workspace_dir).resolve() / "learn_lab"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _runtime(self):
        registry = RuntimeRegistry.shared()
        registry.register_builtin()
        return registry.get("sandbox")

    def _compose_network(self) -> str:
        """Attach the lab to the proxy's network so it can reach litellm."""

        return LlmProxy.shared()._compose_network(DockerEngine.shared())

    def _spec(
        self,
        *,
        purpose: str = _PURPOSE_TEST,
        pull: bool = False,
        workspace_host: str = "",
        workdir: str = "/tmp",
    ) -> RuntimeSpec:
        network = ""
        try:
            network = self._compose_network()
        except Exception:
            network = ""
        authoring = purpose == _PURPOSE_AUTHOR
        return RuntimeSpec(
            name=self.container_name() if authoring else self.test_container_name(),
            image=self.image(),
            role="learn-lab",
            container_workdir=workdir,
            workspace_host=(workspace_host or "").strip(),
            labels={"orchestrator.role": "learn-lab", "orchestrator.learn_lab": purpose},
            network=network,
            pull_image=pull,
            state_dir=str(runtime_state_dir()),
        )

    def status(self) -> dict[str, Any]:
        return self._runtime().status(self._spec())

    def ensure(self) -> SessionInfo:
        """Wipe the test lab and create a fresh container (no leftover tools)."""
        spec = self._spec()
        self._reset(spec)
        session = self._runtime().provision(spec)
        self._bind(session)
        return session.info


    def ensure_for_authoring(self, *, workspace_host: str = "") -> SessionInfo:
        """Ensure a lab with the staging root mounted for OpenCode drafts.

        Reuses a running authoring lab when possible so OpenCode is not
        reinstalled on every suggest/write. Install-test ``ensure()`` still
        wipes to a clean image.
        """
        host = (workspace_host or "").strip() or str(self.staging_root())
        Path(host).mkdir(parents=True, exist_ok=True)
        spec = self._spec(
            purpose=_PURPOSE_AUTHOR,
            workspace_host=host,
            workdir="/workspace",
        )
        runtime = self._runtime()
        st = runtime.status(spec)
        if st.get("running"):
            # Remount requires recreate when the prior lab had no workspace bind.
            session = runtime.attach(spec)
            probe = session.exec(["test", "-d", "/workspace"], timeout=20, shell=False)
            if probe.ok:
                self._bind(session)
                return session.info
            self._reset(spec)
        elif st.get("exists"):
            self._reset(spec)
        session = runtime.provision(spec)
        self._bind(session)
        return session.info

    def create(self) -> dict[str, Any]:
        """Explicit create/start for the Learn UI switch (always clean slate)."""
        info = self.ensure()
        return {
            "ok": True,
            "action": info.action,
            "lab": self.status(),
            "name": info.name,
        }

    def delete(self) -> dict[str, Any]:
        """Remove the test lab. The authoring lab is left running."""
        name = self.test_container_name()
        runtime = self._runtime()
        spec = self._spec()
        st = runtime.status(spec)
        if st.get("error") and not st.get("docker"):
            return {"ok": False, "removed": False, "name": name, "error": st.get("error") or ""}

        result = runtime.destroy(spec)
        if not result.get("ok") and result.get("error"):
            return {
                "ok": False,
                "removed": False,
                "name": name,
                "error": str(result.get("error") or ""),
            }
        removed_any = bool(result.get("removed"))
        removed_any = self._sweep((f"label=orchestrator.learn_lab={_PURPOSE_TEST}",)) or removed_any
        Session.reset()
        if not removed_any:
            return {"ok": True, "removed": False, "name": name, "reason": "not found"}
        return {"ok": True, "removed": True, "name": name}

    def _reset(self, spec: RuntimeSpec) -> None:
        result = self._runtime().destroy(spec)
        if not result.get("ok"):
            raise RuntimeError(
                f"Failed to reset Learn lab {spec.name}: {result.get('error') or 'unknown'}"
            )
        Session.reset()

    def _sweep(self, filters: tuple[str, ...]) -> bool:
        runtime = self._runtime()
        removed = False
        image = self.image()
        for label in filters:
            for cid in runtime.find(label):
                runtime.destroy(RuntimeSpec(name=cid, role="learn-lab", image=image))
                removed = True
        return removed

    def _bind(self, session) -> None:
        """Bind session + job workdir so exec/provision use the lab cwd."""
        bind_runtime_session(session, default_workdir=session.info.workdir or "/tmp")

    def connect(self, *, authoring: bool = False) -> RuntimeSession:
        """Bind the session to a lab that is already running."""
        spec = self._spec(
            purpose=_PURPOSE_AUTHOR if authoring else _PURPOSE_TEST,
            workdir="/workspace" if authoring else "/tmp",
        )
        st = self._runtime().status(spec)
        if not st.get("running"):
            raise RuntimeError(f"Learn lab {spec.name!r} is not running.")
        session = self._runtime().attach(spec)
        self._bind(session)
        return session

    def test_tool_install(self, *, yaml_text: str, install_script: str = "") -> dict[str, Any]:
        """Run catalog provision for one recipe inside the Learn lab."""

        text = (yaml_text or "").strip()
        if not text:
            raise ValueError("yaml is required")
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("tool YAML must be a mapping")

        tid = str(raw.get("id") or "").strip().lower()
        if not is_kebab(tid):
            raise ValueError("tool id is required and must be a simple slug")

        with tempfile.TemporaryDirectory(prefix=f"{tid}-", dir=str(self.staging_root())) as tmp:
            catalog_dir = Path(tmp)
            (catalog_dir / f"{tid}.yaml").write_text(text + "\n", encoding="utf-8")
            script = (install_script or "").strip()
            if script:
                (catalog_dir / f"{tid}.sh").write_text(script + "\n", encoding="utf-8")
            catalog = ToolCatalog(root=catalog_dir)
            log_lines: list[str] = []
            try:
                info = self.ensure()
            except RuntimeError as exc:
                return {
                    "ok": False,
                    "verified": False,
                    "message": str(exc),
                    "log": "",
                    "lab": self.status(),
                    "tool_id": tid,
                }
            log_lines.append(f"lab={info.name} image={info.image} action={info.action}")

            missing_shell = self._ensure_shell_basics()
            if missing_shell:
                log_lines.append(f"bootstrap failed: {missing_shell}")
                return {
                    "ok": False,
                    "verified": False,
                    "message": f"lab bootstrap failed: {missing_shell}",
                    "log": "\n".join(log_lines),
                    "lab": self.status(),
                    "tool_id": tid,
                }

            tool = catalog.by_id(tid)
            if tool is None:
                return {
                    "ok": False,
                    "verified": False,
                    "message": f"catalog did not load tool {tid!r}",
                    "log": "\n".join(log_lines),
                    "lab": self.status(),
                    "tool_id": tid,
                }

            ok, msg = CatalogProvisioner(catalog=catalog).provision_binary(tool.binary or tool.id)
            log_lines.append(msg)
            verify = CatalogProvisioner.run_verify(tool)
            log_lines.append("--- verify ---")
            log_lines.append(str(verify.get("output") or ""))
            verified = bool(verify.get("ok"))
            return {
                "ok": bool(ok and verified),
                "verified": verified,
                "message": msg,
                "log": "\n".join(log_lines),
                "verify_output": str(verify.get("output") or ""),
                "verify_steps": verify.get("steps") or [],
                "lab": self.status(),
                "tool_id": tid,
            }

    @staticmethod
    def _ensure_shell_basics() -> str:
        """Install curl and bash only when the image does not already have them."""
        probe = Session.current().exec(
            "command -v curl >/dev/null && command -v bash >/dev/null",
            timeout=20,
            shell=True,
        )
        if probe.ok:
            return ""
        bootstrap = Session.current().exec(
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && "
            "apt-get install -y --no-install-recommends ca-certificates curl bash "
            ">/dev/null",
            timeout=300,
            shell=True,
        )
        if bootstrap.code == 0:
            return ""
        return (bootstrap.stderr or bootstrap.stdout or "").strip()[:500] or "bootstrap failed"
