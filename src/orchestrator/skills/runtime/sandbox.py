"""The only agent command path: the sandbox bound for this Dramatiq job.

Claude ``bash`` and ``script`` (argv) are the implementation. Capability
wrappers (``provision_cli``, ``sandbox_status``, ``run_periodic``, skill
scripts) call those. Host execution is refused when no session is bound.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_runtime.api import Session
from orchestrator.agent.job import get_job
from orchestrator.crew.roles.registry import RoleRegistry
from orchestrator.sandbox.shell import ProvisionService, ShellRunner
from orchestrator.sandbox.tool_output import ShellResult, render_tool_output
from orchestrator.skills.providers.base import CLAUDE_PROVIDER, PEON_PROVIDER, SkillDocument
from orchestrator.skills.registry import SkillRegistry
from orchestrator.skills.runtime.interp import interpreter_argv
from orchestrator.tools.install import InstallResolver
from orchestrator.utils.commands import as_shell_cli, known_cli_names
from orchestrator.utils.paths import normalize_rel_path, safe_join
from orchestrator.utils.stream import StreamEmitter
from orchestrator.utils.stream_events import (
    EVENT_PROVISION_CLI,
    EVENT_RUN_CLI,
    EVENT_RUN_PERIODIC,
    cli_payload,
    envelope,
)

logger = logging.getLogger(__name__)

_RUNTIMES = frozenset({"python3", "python", "bash", "sh", "node"})


def require_bound() -> str | None:
    return Session.require_bound()


def emit(kind: str, content: str, **metadata: object) -> None:
    get_job().bridge.emit(kind, content, metadata=dict(metadata) if metadata else None)


def emit_event(
    kind: str,
    content: str,
    event: str,
    payload: Mapping[str, Any] | None = None,
    **meta: object,
) -> None:
    emit(kind, content, **envelope(event, payload, **meta))


def job_role_id(*, required: bool = True) -> str:
    """Role id for the bound job. When ``required`` is false, missing scope is \"\"."""
    try:
        extras = getattr(get_job(), "extras", None) or {}
    except RuntimeError:
        if required:
            raise
        return ""
    return str(extras.get("role_id") or "").strip().lower()


def role_binary_denial(binary: str) -> str:
    """Explain a catalog ownership mismatch, including valid handoff roles."""
    name = (binary or "").strip().lower()
    role_id = job_role_id()
    if not name or not role_id:
        return ""
    registry = RoleRegistry.shared()
    role = registry.get(role_id)
    if role is None:
        return ""
    from orchestrator.skills.bind import effective_binaries, resolve_skill_ids

    extras = getattr(get_job(), "extras", None) or {}
    allowed = effective_binaries(role, resolve_skill_ids(role, extras.get("skill_ids")))
    if not allowed or name in allowed:
        return ""
    owners = []
    for candidate in registry.list_roles():
        if candidate.is_manager:
            continue
        if name in effective_binaries(candidate, candidate.skills):
            owners.append(candidate.id)
    handoff = (
        " Use spawn_work with one of the catalog owners: " + ", ".join(owners)
        if owners
        else " No catalog role currently owns this binary."
    )
    return (
        f"binary {name!r} not allowlisted for role {role_id}. "
        f"Allowed: {', '.join(allowed)}.{handoff}"
    )


def _command_binary(command: str) -> str:
    shell = as_shell_cli(command)
    if not shell:
        return ""
    return shell.split(None, 1)[0].strip().lower()


def _gate(command: str) -> str | None:
    """RoE and duplicate-scan refusal. ``None`` means allowed."""
    cmd = (command or "").strip()
    if not cmd:
        return None
    bridge = get_job().bridge
    blocked = bridge.assert_command_allowed(cmd)
    if blocked:
        return f"Error: RoE blocked — {blocked}"
    checker = getattr(bridge, "duplicate_scan_reason", None)
    if callable(checker):
        dup = checker(cmd) or ""
        if dup:
            return f"Skipped: {dup}"
    return None


def _catalog_files(doc: SkillDocument) -> dict[str, str]:
    try:
        from orchestrator.catalog import PlatformCatalog

        rec = PlatformCatalog.shared().get_skill(doc.id)
    except Exception:
        return {}
    if rec is None:
        return {}
    return dict(rec.files)


def container_skill_root(doc: SkillDocument) -> str:
    if doc.container_root:
        return doc.container_root
    dirname = doc.root.name if doc.root is not None else doc.name
    if doc.provider == CLAUDE_PROVIDER:
        return f"/skills-imported/{dirname}"
    if doc.provider == PEON_PROVIDER:
        return f"/skills/{dirname}"
    return f"/skills-{doc.provider}/{dirname}"


def _skill_cache_name(doc: SkillDocument) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", doc.id).strip("-") or "skill"


def _setup_manifest(doc: SkillDocument) -> str:
    setup = doc.setup
    if setup is None:
        return ""
    if setup.manifest:
        return setup.manifest
    return "requirements.txt" if setup.kind == "pip" else "package.json"


def _setup_fingerprint(doc: SkillDocument, files: dict[str, str] | None = None) -> str:
    """Hash dependency metadata so workspace caches follow skill changes."""
    setup = doc.setup
    if setup is None:
        return ""
    if files is not None:
        manifest_name = _setup_manifest(doc)
        content = files.get(manifest_name)
        if content is None and setup.cwd not in {"", "."}:
            content = files.get(f"{setup.cwd}/{manifest_name}")
        if content is None:
            return ""
        digest = hashlib.sha256()
        digest.update(setup.kind.encode("utf-8"))
        digest.update(b"\0")
        digest.update(manifest_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        return digest.hexdigest()
    if doc.root is None:
        return ""
    root = (Path(doc.root) / (setup.cwd or ".")).resolve()
    names = [_setup_manifest(doc)]
    if setup.kind == "pip":
        optional_names = (
            "requirements.lock",
            "constraints.txt",
            "uv.lock",
            "poetry.lock",
        )
    else:
        optional_names = ("package-lock.json", "npm-shrinkwrap.json")
    digest = hashlib.sha256()
    digest.update(setup.kind.encode("utf-8"))
    for name in names:
        path = root / name
        try:
            content = path.read_bytes()
        except OSError:
            return ""
        digest.update(b"\0")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    for name in optional_names:
        path = root / name
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            return ""
        digest.update(b"\0")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _setup_marker_matches(session, marker: str, fingerprint: str) -> bool:
    if not fingerprint:
        return False
    result = session.exec(["cat", marker], timeout=15)
    return result.ok and result.stdout.strip() == fingerprint


def _write_setup_marker(session, marker: str, fingerprint: str):
    if not fingerprint:
        return True
    result = session.exec(
        [
            "sh",
            "-c",
            f"printf '%s\\n' {shlex.quote(fingerprint)} > {shlex.quote(marker)}",
        ],
        timeout=15,
    )
    return result.ok


def _run_setup_post(
    session, setup, setup_root: str, env: dict[str, str], *, path_prefix: str
) -> str:
    """Run the setup commands a skill declares (``setup-post``), env first on PATH."""
    for command in tuple(getattr(setup, "post", ()) or ()):
        done = session.exec(
            ["sh", "-c", command],
            cwd=setup_root,
            timeout=1200,
            env={**env, "PATH": f"{path_prefix}:/usr/local/bin:/usr/bin:/bin"},
        )
        if not done.ok:
            return _exec_error(done, f"setup command failed: {command[:80]}")
    return ""


def _ensure_skill_setup(
    doc: SkillDocument,
    session,
    files: dict[str, str] | None = None,
) -> tuple[str, dict[str, str], str]:
    """Install declared Python/Node dependencies into the writable workspace."""
    setup = doc.setup
    if setup is None or setup.kind not in {"pip", "npm"}:
        return "", {}, ""
    setup_root = doc.container_setup_root or container_skill_root(doc)
    manifest = f"{setup_root}/{_setup_manifest(doc)}"
    fingerprint = _setup_fingerprint(doc, files)
    env = {"PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    if setup.kind == "pip":
        cache = f"/workspace/.peon-skill-venvs/{_skill_cache_name(doc)}"
        python = f"{cache}/bin/python"
        marker = f"{cache}/.peon-ready"
        if _setup_marker_matches(session, marker, fingerprint):
            return python, env, ""
        if session.exec(["test", "-e", cache], timeout=15).ok:
            removed = session.exec(["rm", "-rf", cache], timeout=30)
            if not removed.ok:
                return "", env, _exec_error(removed, "stale venv removal failed")
        created = session.exec(["python3", "-m", "venv", cache], timeout=180)
        if not created.ok:
            return "", env, _exec_error(created, "venv creation failed")
        if _setup_manifest(doc) == "pyproject.toml":
            install_argv = [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-e",
                setup_root,
            ]
        else:
            install_argv = [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                manifest,
            ]
        installed = session.exec(
            install_argv,
            cwd=setup_root,
            timeout=1200,
            env=env,
        )
        if not installed.ok:
            return "", env, _exec_error(installed, "dependency install failed")
        post_error = _run_setup_post(session, setup, setup_root, env, path_prefix=f"{cache}/bin")
        if post_error:
            return "", env, post_error
        if not _write_setup_marker(session, marker, fingerprint):
            return "", env, "setup marker failed"
        return python, env, ""

    if not session.which("npm"):
        return "", env, "npm is not available in the sandbox image"
    cache = f"/workspace/.peon-skill-node/{_skill_cache_name(doc)}"
    marker = f"{cache}/.peon-ready"
    env["NODE_PATH"] = f"{cache}/node_modules"
    if _setup_marker_matches(session, marker, fingerprint):
        return "", env, ""
    if session.exec(["test", "-e", cache], timeout=15).ok:
        removed = session.exec(["rm", "-rf", cache], timeout=30)
        if not removed.ok:
            return "", env, _exec_error(removed, "stale Node setup removal failed")
    made = session.exec(["mkdir", "-p", cache], timeout=15)
    if not made.ok:
        return "", env, _exec_error(made, "Node setup directory creation failed")
    copied = session.exec(
        ["cp", manifest, f"{cache}/package.json"],
        timeout=15,
    )
    if not copied.ok:
        return "", env, _exec_error(copied, "package.json copy failed")
    for lockfile in ("package-lock.json", "npm-shrinkwrap.json"):
        lock_source = f"{setup_root}/{lockfile}"
        if session.exec(["test", "-f", lock_source], timeout=15).ok:
            session.exec(["cp", lock_source, f"{cache}/{lockfile}"], timeout=15)
            break
    installed = session.exec(
        ["npm", "install", "--prefix", cache, "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=setup_root,
        timeout=1200,
        env=env,
    )
    if not installed.ok:
        return "", env, _exec_error(installed, "Node dependency install failed")
    post_error = _run_setup_post(
        session, setup, setup_root, env, path_prefix=f"{cache}/node_modules/.bin"
    )
    if post_error:
        return "", env, post_error
    if not _write_setup_marker(session, marker, fingerprint):
        return "", env, "setup marker failed"
    return "", env, ""


def _exec_error(result, fallback: str) -> str:
    return (result.stderr or result.stdout or fallback).strip()


def _configured_skill_env(session) -> dict[str, str]:
    """Forward only operator-selected environment keys into skill scripts."""
    raw = session.env_get("SKILLS_FORWARD_ENV")
    if not raw:
        return {}
    values: dict[str, str] = {}
    for item in re.split(r"[,:;\s]+", raw):
        key = item.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = session.env_get(key)
        if value:
            values[key] = value
    return values


class SandboxTools:
    """Status, provision, shell, periodic watch, and skill scripts."""

    def ready(self) -> str:
        err = require_bound()
        if err:
            return err
        return "Sandbox ready — " + self.status()

    def status(self, *, extra_binaries: bool = False) -> str:
        err = require_bound()
        if err:
            return err
        info = Session.current().info
        lines = [f"name={info.name} mode={info.mode} session_id={info.project_id or '-'}"]
        if extra_binaries:
            try:
                which = Session.current().which
                for binary in ("python3", "bash", "curl", "apt-get"):
                    lines.append(f"{binary}={'yes' if which(binary) else 'no'}")
            except Exception as exc:
                logger.warning("sandbox binary probe failed: %s", exc, exc_info=True)
        return "\n".join(lines)

    def provision(
        self,
        binary: str,
        package: str = "",
        *,
        enforce_role_allowlist: bool = False,
    ) -> str:
        err = require_bound()
        if err:
            return err
        name = (binary or "").strip()
        if not name:
            return "Error: provide a binary name."
        if enforce_role_allowlist:
            denial = role_binary_denial(name)
            if denial:
                return f"Error: {denial}"
        emit_event(
            "log",
            f"provision {name}",
            EVENT_PROVISION_CLI,
            {"binary": name, "package": package or "", "phase": "start"},
        )
        ok, msg = InstallResolver.shared().resolve(name, package=package)
        if not ok:
            return f"Error: {msg}"
        text = msg or f"provisioned {name}"
        emit_event(
            "log",
            text,
            EVENT_PROVISION_CLI,
            {"binary": name, "package": package or "", "phase": "done"},
        )
        return text

    def _prepare_shell(self, command: str) -> tuple[str | None, str]:
        """Return ``(cmd, \"\")`` when runnable, or ``(None, error)``."""
        err = require_bound()
        if err:
            return None, err
        cmd = (command or "").strip()
        if not cmd:
            return None, "Error: empty command."
        cli = _command_binary(cmd)
        if cli:
            denial = role_binary_denial(cli)
            if denial:
                return None, f"Error: {denial}"
        blocked = _gate(cmd)
        if blocked:
            return None, blocked
        return cmd, ""

    def bash(self, command: str) -> str:
        """Claude Bash tool."""
        cmd, err = self._prepare_shell(command)
        if err:
            return err
        assert cmd is not None
        emit_event(
            "tool",
            f"bash: {cmd[:200]}",
            EVENT_RUN_CLI,
            cli_payload(cmd, phase="start"),
        )
        result = ShellRunner.shared().run_shell_result(cmd)
        emit_event(
            "result",
            f"bash done exit={result.code}",
            EVENT_RUN_CLI,
            cli_payload(cmd, phase="done", exit_code=result.code),
        )
        return render_tool_output(Session.current(), result, label="bash")

    def periodic(
        self,
        command: str,
        interval_seconds: int = 30,
        duration_seconds: int = 120,
        package: str = "",
    ) -> str:
        try:
            interval = int(interval_seconds)
            duration = int(duration_seconds)
        except (TypeError, ValueError):
            return "Error: interval_seconds and duration_seconds must be integers."
        cmd, err = self._prepare_shell(command)
        if err:
            return err
        assert cmd is not None
        emit_event(
            "tool",
            f"run_periodic({interval}s/{duration}s): {cmd[:160]}",
            EVENT_RUN_PERIODIC,
            cli_payload(
                cmd,
                interval_seconds=interval,
                duration_seconds=duration,
                package=package or "",
                phase="start",
            ),
        )
        result = ShellRunner.shared().run_periodic_result(
            cmd,
            interval_seconds=interval,
            duration_seconds=duration,
            package=package or "",
        )
        emit_event(
            "result",
            f"run_periodic done exit={result.code}",
            EVENT_RUN_PERIODIC,
            cli_payload(cmd, phase="done", exit_code=result.code),
        )
        return render_tool_output(Session.current(), result, label="periodic")

    def script(
        self,
        skill_id: str,
        relative_entry: str,
        args: Sequence[str] | None = None,
    ) -> str:
        """Run a catalogued skill script with an immutable pack and workspace cwd."""
        err = require_bound()
        if err:
            return err
        mode = (Session.current().info.mode or "").strip().lower()
        if mode == "openshell":
            return (
                "Error: skill scripts need Docker sandbox mounts "
                "(/skills, /skills-imported); OpenShell does not provide them."
            )
        try:
            doc = SkillRegistry.shared().require(skill_id)
        except KeyError as exc:
            return f"Error: {exc}"
        role_id = job_role_id(required=False)
        blocked = doc.access_error(role_id=role_id)
        if blocked:
            return blocked
        rel = normalize_rel_path(relative_entry)
        if not rel:
            return f"Error: script {relative_entry!r} is not a file in {doc.id}."
        files = _catalog_files(doc)
        if rel in files:
            from orchestrator.skills.runtime.interp import interpreter_for_source

            prefix = interpreter_for_source(rel, files[rel])
        elif doc.root is not None:
            host = safe_join(doc.root, rel)
            if host is None or not host.is_file():
                return f"Error: script {relative_entry!r} is not a file in {doc.id}."
            prefix = interpreter_argv(host)
        else:
            return f"Error: script {relative_entry!r} is not a file in {doc.id}."
        if prefix is None:
            return f"Error: no interpreter for {rel}."
        script_args = [str(part) for part in (args or ())]
        tail_line = shlex.join(script_args)
        # Only a catalog CLI at the start of the argv is a shell line.
        # A Claude script argument such as ``notes.txt`` must not be installed.
        binary = ProvisionService.command_binary(tail_line) if tail_line else ""
        if binary and binary.lower() in known_cli_names():
            denial = role_binary_denial(binary)
            if denial:
                return f"Error: {denial}"
            ok, msg = ProvisionService.shared().provision_command(tail_line)
            if not ok:
                return f"Error: {msg}"
        session = Session.current()
        root = container_skill_root(doc)
        if files:
            from orchestrator.sandbox.materialize import materialize_tree

            wrote = materialize_tree(session, root, files)
            if wrote:
                return wrote
        elif doc.root is None:
            return f"Error: skill {doc.id} has no files in the platform catalog."
        setup_python, setup_env, setup_error = _ensure_skill_setup(doc, session, files or None)
        if setup_error:
            return f"Error: could not prepare {doc.id}: {setup_error[:2000]}"
        if setup_python and prefix and Path(prefix[0]).name in {"python", "python3"}:
            prefix = [setup_python, *prefix[1:]]
        entry = f"{root}/{rel}"
        argv = [*prefix, entry, *script_args]
        runtime_bin = Path(prefix[0]).name if prefix else ""
        if (
            runtime_bin in _RUNTIMES
            and not Path(prefix[0]).is_absolute()
            and not session.which(runtime_bin)
        ):
            provisioned = self.provision(runtime_bin)
            if provisioned.startswith("Error"):
                return provisioned
        # Gate the operator argv first; the interpreter-prefixed line only when
        # there was no separate shell line (pure Claude args / no args).
        joined = shlex.join(argv)
        for probe in (tail_line,) if tail_line else (joined,):
            gated = _gate(probe)
            if gated:
                return gated
        skill_env = {
            "SKILL_DIR": root,
            "SKILL_WORKDIR": session.workdir(),
            "ORCHESTRATOR_SKILL_NAME": doc.name,
        }
        skill_env.update(_configured_skill_env(session))
        skill_env.update(setup_env)
        emit_event(
            "tool",
            f"run_skill_script: {doc.id} {rel}",
            EVENT_RUN_CLI,
            cli_payload(
                tail_line or joined,
                skill_id=doc.id,
                entry=rel,
                argv=argv,
                phase="start",
            ),
        )
        # Pack mounts are immutable. Relative output belongs in the writable
        # project workspace; SKILL_DIR remains available for pack resources.
        # Long OSINT / Playwright packs need well over 30 minutes.
        result = session.exec(argv, cwd=session.workdir(), timeout=2400, env=skill_env)
        emit_event(
            "result",
            f"run_skill_script done exit={result.code}",
            EVENT_RUN_CLI,
            cli_payload(
                tail_line or joined,
                skill_id=doc.id,
                entry=rel,
                phase="done",
                exit_code=result.code,
            ),
        )
        out = (result.stdout or "").strip()
        err_out = (result.stderr or "").strip()
        stream = StreamEmitter.shared()
        if out:
            stream.emit_stdout(out)
        if err_out:
            stream.emit_stderr(err_out)
        return render_tool_output(
            session,
            ShellResult(result.code if result.code is not None else 1, out, err_out),
            label=f"skill-{doc.name}",
        )
