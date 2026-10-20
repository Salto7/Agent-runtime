"""Start and call MCP servers inside the bound project sandbox.

The worker does not speak MCP. It materializes the catalog row and talks to
a supervisor that uses the official ``mcp`` Python SDK (``ClientSession`` +
``stdio_client``) inside the same container as the servers.

Supervisor SoT: ``supervisor_runtime.py``, baked at ``/opt/orchestrator-mcp/supervisor.py``.
Stale/missing image copies are materialized to ``/workspace/.peon/mcp/supervisor.py``.

Large tool/resource results spill to ``workspace/mcp/artifacts/``; this module
returns envelopes (preview + path), never a silent payload clip.

Child env always includes ``PEON_FINDINGS_QUEUE`` so servers may append
Finding-shaped NDJSON for job-end ingest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent_runtime.api import Session
from orchestrator.sandbox.materialize import materialize_tree
from orchestrator.skills.runtime.sandbox import emit_event, job_role_id
from orchestrator.utils.stream_events import (
    EVENT_CALL_MCP_TOOL,
    EVENT_LIST_MCP_RESOURCES,
    EVENT_LIST_MCP_TOOLS,
    EVENT_READ_MCP_RESOURCE,
)

logger = logging.getLogger(__name__)

_SOCK = "/tmp/orchestrator-mcp/supervisor.sock"
_PID = "/tmp/orchestrator-mcp/supervisor.pid"
_STAMP = "/workspace/.peon/mcp/supervisor.sha"
_IMAGE_PYTHON = "/opt/orchestrator-mcp-client/bin/python"
_IMAGE_SUPERVISOR = "/opt/orchestrator-mcp/supervisor.py"
_WORKSPACE_SUPERVISOR = "/workspace/.peon/mcp/supervisor.py"
_PACKAGE_SUPERVISOR = Path(__file__).with_name("supervisor_runtime.py")
_FALLBACK_VENV = "/workspace/.peon/mcp-venvs/_mcp_client"
_FALLBACK_PYTHON = f"{_FALLBACK_VENV}/bin/python"
_FALLBACK_MARKER = f"{_FALLBACK_VENV}/.orchestrator-mcp-sdk"
# Supervisor uses ClientSession/stdio_client; verified on 1.x and 2.x.
_MCP_SDK_SPEC = "mcp>=1.10,<3"
_MCP_IMPORT = "from mcp import ClientSession, StdioServerParameters"
_FINDINGS_QUEUE = "/workspace/workspace/findings_queue.jsonl"
_ARTIFACT_DIR = "/workspace/workspace/mcp/artifacts"
_PATH_RUNNERS = frozenset({"node", "npm", "npx", "uv", "uvx"})
_PYTHON_RUNNERS = frozenset({"python", "python3"})
_INSTALL_KINDS = frozenset({"pip", "uv", "npm"})
# (container name, mcp_id) -> revision already materialized + installed there.
# A recreated container under the same name still fails safe: the install
# markers in the workspace are re-checked whenever the revision differs.
_PREPARED: dict[tuple[str, str], str] = {}
# Must match the supervisor's PEON_MCP_STARTUP_SECONDS default (cold uv/npx start).
_STARTUP_SECONDS = 600
_OP_EVENTS = {
    "list_tools": EVENT_LIST_MCP_TOOLS,
    "call": EVENT_CALL_MCP_TOOL,
    "list_resources": EVENT_LIST_MCP_RESOURCES,
    "read_resource": EVENT_READ_MCP_RESOURCE,
}
# Thin NDJSON client: worker → supervisor Unix socket (not MCP framing).
_SOCK_RPC = r"""
import json, socket, sys
raw = sys.stdin.read()
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(float(sys.argv[2]))
sock.connect(sys.argv[1])
sock.sendall(raw.encode("utf-8") + b"\n")
buf = b""
while b"\n" not in buf:
    chunk = sock.recv(65536)
    if not chunk:
        break
    buf += chunk
    if len(buf) > 8_000_000:
        break
sys.stdout.write(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
"""


def bound_mcp_ids(role_id: str) -> tuple[str, ...]:
    """MCP server ids bound to ``role_id`` (empty when unbound / unknown)."""
    return tuple(rec.mcp_id for rec in _bound(role_id))


def list_mcp_tools(name: str) -> str:
    return _invoke(name, op="list_tools")


def call_mcp_tool(name: str, tool: str, arguments_json: str = "{}") -> str:
    tool_name = str(tool or "").strip()
    if not tool_name:
        return "Error: tool name is required."
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return "Error: arguments_json must be a JSON object."
    if not isinstance(arguments, dict):
        return "Error: arguments_json must be a JSON object."
    return _invoke(name, op="call", tool=tool_name, arguments=arguments)


def list_mcp_resources(name: str) -> str:
    return _invoke(name, op="list_resources")


def read_mcp_resource(name: str, uri: str) -> str:
    resource = str(uri or "").strip()
    if not resource:
        return "Error: resource uri is required."
    return _invoke(name, op="read_resource", uri=resource)


def mcp_prompt_block(role_id: str) -> str:
    """Short index of MCP servers bound to ``role_id``."""
    records = _bound(role_id)
    if not records:
        return ""
    lines = [
        "## MCP servers",
        "Prefer these over re-implementing the same work with bash, ad-hoc "
        "scrapers, or invented inventories. "
        "Call `list_mcp_tools` then `call_mcp_tool` for each bound server. "
        "Servers run inside this project sandbox. "
        "Large tool results are saved under `workspace/mcp/artifacts/`; "
        "the tool return is a preview plus an `artifact` path — "
        "use `read_workspace_artifact` or `read_mcp_resource` for full evidence. "
        "Servers may also append Finding rows to the project findings queue; "
        "prefer `list_findings` over pasting bulk rows through `record_findings`. "
        "Endpoint values are project configuration, not something you set. "
        "RoE `asset:` names and seed intent subjects authorize passive collection.",
    ]
    for rec in records:
        required = ", ".join(name for name, req in rec.env_schema if req) or "(none)"
        lines.append(
            f"- `{rec.mcp_id}`: {rec.description or rec.command}. Required env: {required}."
        )
    return "\n".join(lines)


def ensure_mcp_runtime(session=None) -> str:
    """Warm the in-sandbox MCP supervisor (SDK + process). Empty string on success."""
    if session is None:
        err = Session.require_bound()
        if err:
            return err
        session = Session.current()
    mode = (session.info.mode or "").strip().lower()
    if mode == "openshell":
        return ""
    return _ensure_supervisor(session)


def _invoke(
    name: str,
    *,
    op: str,
    tool: str = "",
    arguments: dict | None = None,
    uri: str = "",
) -> str:
    err = Session.require_bound()
    if err:
        return err
    session = Session.current()
    if (session.info.mode or "").strip().lower() == "openshell":
        return "Error: MCP servers need the Docker sandbox."
    rec = _record_for_role(name, job_role_id(required=False))
    if isinstance(rec, str):
        return rec
    project_id = str(session.info.project_id or "").strip()
    env, missing = _project_env(project_id, rec)
    if missing:
        return (
            f"Error: MCP server {rec.mcp_id!r} is not configured for this project. "
            f"Missing: {', '.join(missing)}. "
            "Set values under Project settings → MCP servers "
            "(use host.docker.internal for host endpoints)."
        )
    dest = f"/workspace/.peon/mcp/{rec.mcp_id}"
    install, install_error = _effective_install(rec)
    if install_error:
        return install_error
    revision = _install_digest(rec, install)
    prepared = _prepare(session, rec, dest, install, revision)
    if prepared:
        return prepared
    command, args = _runtime_command(rec, dest, install)
    timeout = 180 if op in {"call", "read_resource"} else 60
    request: dict[str, Any] = {
        "op": op,
        "mcp_id": rec.mcp_id,
        "command": command,
        "args": list(args),
        "cwd": dest if not rec.cwd else f"{dest}/{rec.cwd}",
        "env": {**env, "PEON_FINDINGS_QUEUE": _FINDINGS_QUEUE},
        "timeout": timeout,
        "revision": revision,
    }
    if op == "call":
        request["tool"] = tool
        request["arguments"] = arguments or {}
    elif op == "read_resource":
        request["uri"] = uri
    _emit_mcp(
        op,
        rec.mcp_id,
        tool=tool,
        uri=uri,
        reply={"ok": True, "result": {}},
        arguments=arguments,
        phase="start",
    )
    reply = _sock_rpc(session, request)
    _emit_mcp(
        op,
        rec.mcp_id,
        tool=tool,
        uri=uri,
        reply=reply,
        arguments=arguments,
        phase="done",
    )
    if not reply.get("ok"):
        # Re-materialize next time in case the workspace copy was removed.
        _PREPARED.pop((session.info.name, rec.mcp_id), None)
        return f"Error: {reply.get('error') or 'MCP call failed'}"
    if op == "list_tools":
        return _format_catalog(rec.mcp_id, "tools", reply.get("tools") or [])
    if op == "list_resources":
        return _format_catalog(rec.mcp_id, "resources", reply.get("resources") or [])
    return json.dumps(reply.get("result"), default=str)


def _prepare(session, rec, dest: str, install: dict | None, revision: str) -> str:
    """Materialize files, install deps, and start the supervisor.

    Files and deps are only rewritten when ``revision`` changes for this
    container; repeat calls cost one supervisor liveness probe.
    """
    key = (session.info.name, rec.mcp_id)
    if _PREPARED.get(key) != revision:
        wrote = materialize_tree(session, dest, dict(rec.files))
        if wrote:
            return wrote
        installed = _install(session, rec, dest, install)
        if installed:
            return installed
    started = _ensure_supervisor(session)
    if started:
        _PREPARED.pop(key, None)
        return started
    _PREPARED[key] = revision
    return ""


def _emit_mcp(
    op: str,
    mcp_id: str,
    *,
    tool: str = "",
    uri: str = "",
    reply: dict,
    arguments: dict | None = None,
    phase: str = "done",
) -> None:
    event = _OP_EVENTS.get(op)
    if not event:
        return
    result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    payload: dict[str, Any] = {
        "mcp_id": mcp_id,
        "op": op,
        "ok": bool(reply.get("ok")),
        "phase": phase or "done",
    }
    if tool:
        payload["tool"] = tool
    if arguments:
        # What the agent asked for (stage skips, limits, targets) — the audit
        # trail must answer "why did this call return what it did".
        payload["arguments"] = arguments
    if uri:
        payload["uri"] = uri
    if isinstance(result, dict) and result.get("artifact"):
        payload["artifact"] = result.get("artifact")
        payload["bytes"] = result.get("bytes")
    if not reply.get("ok"):
        payload["error"] = str(reply.get("error") or "")[:400]
    try:
        emit_event("tool", f"mcp:{mcp_id}:{tool or uri or op}", event, payload)
    except Exception:
        logger.warning("mcp stream emit skipped", exc_info=True)


def _format_catalog(mcp_id: str, kind: str, items: list) -> str:
    if not items:
        return f"{mcp_id}: no {kind}"
    lines = [f"{mcp_id} {kind}:"]
    for item in items:
        if not isinstance(item, dict):
            continue
        if kind == "tools":
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            desc = str(item.get("description") or "").strip()
            lines.append(f"- {name}" + (f": {desc}" if desc else ""))
            continue
        uri = str(item.get("uri") or "").strip()
        if not uri:
            continue
        name = str(item.get("name") or "").strip()
        desc = str(item.get("description") or "").strip()
        label = f"{name} — " if name else ""
        lines.append(f"- {label}{uri}" + (f": {desc}" if desc else ""))
    return "\n".join(lines)


def bound_mcp_records(role_id: str):
    """MCP server records bound to ``role_id`` (catalog bindings, score >= 0.5)."""
    return _bound(role_id)


def _bound(role_id: str):
    rid = (role_id or "").strip().lower()
    if not rid:
        return []
    try:
        from orchestrator.catalog import PlatformCatalog

        catalog = PlatformCatalog.shared()
        wanted = {
            b.owner_id
            for b in catalog.list_bindings(role_id=rid, owner_kind="mcp")
            if b.score >= 0.5
        }
        return [rec for rec in catalog.list_mcps() if rec.enabled and rec.mcp_id in wanted]
    except Exception as exc:
        logger.warning("mcp bindings lookup failed for role %s: %s", rid, exc)
        return []


def _record_for_role(name: str, role_id: str):
    mid = str(name or "").strip().lower()
    if not mid:
        return "Error: MCP server name is required."
    if not role_id:
        return "Error: MCP calls require a bound role."
    bound = _bound(role_id)
    if not any(rec.mcp_id == mid for rec in bound):
        known = ", ".join(rec.mcp_id for rec in bound) or "(none)"
        return f"Error: MCP server {mid!r} is not bound to role {role_id}. Bound: {known}."
    try:
        from orchestrator.catalog import PlatformCatalog

        full = PlatformCatalog.shared().get_mcp(mid)
    except Exception as exc:
        return f"Error: {exc}"
    if full is None or not full.enabled:
        return f"Error: MCP server {mid!r} is disabled."
    return full


def _project_env(project_id: str, rec) -> tuple[dict[str, str], list[str]]:
    stored: dict[str, str] = {}
    if project_id:
        try:
            from orchestrator.catalog.models import ProjectMcpConfig

            row = ProjectMcpConfig.objects.filter(
                project_id=project_id, mcp_id=rec.mcp_id
            ).first()
            if row is not None and isinstance(row.env, dict):
                stored = {
                    str(k): str(v).strip()
                    for k, v in row.env.items()
                    if v is not None and str(v).strip()
                }
        except Exception as exc:
            logger.warning("mcp project env read failed for %s: %s", rec.mcp_id, exc)
            return {}, [f"project configuration ({exc})"]
    missing = [name for name, required in rec.env_schema if required and not stored.get(name)]
    allowed = {name for name, _req in rec.env_schema}
    env = {key: value for key, value in stored.items() if not allowed or key in allowed}
    return env, missing


def _command_name(command: str) -> str:
    return str(command or "").strip().replace("\\", "/").rsplit("/", 1)[-1]


def _effective_install(rec) -> tuple[dict | None, str]:
    """Use the catalog install, or infer one from the server's own files."""
    install = dict(rec.install) if isinstance(rec.install, dict) else {}
    kind = str(install.get("kind") or "")
    if kind in _INSTALL_KINDS:
        return install, ""
    files = rec.files or {}
    has_req = "requirements.txt" in files
    has_pyproject = "pyproject.toml" in files
    has_npm = "package.json" in files
    base = _command_name(rec.command)
    if (has_req or has_pyproject) and has_npm:
        if base in _PYTHON_RUNNERS:
            has_npm = False
        elif base in _PATH_RUNNERS:
            has_req = has_pyproject = False
        else:
            return None, (
                "Error: this MCP server has both Python and Node manifests. "
                "Set install.kind to pip, uv, or npm."
            )
    if has_pyproject:
        return {"kind": "uv", "manifest": "pyproject.toml"}, ""
    if has_req:
        return {"kind": "pip", "manifest": "requirements.txt"}, ""
    if has_npm:
        return {"kind": "npm", "manifest": "package.json"}, ""
    return None, ""


def _venv_bin(mcp_id: str) -> str:
    return f"/workspace/.peon/mcp-venvs/{mcp_id}/bin"


def _runtime_command(rec, dest: str, install: dict | None) -> tuple[str, tuple[str, ...]]:
    install = install or {}
    kind = str(install.get("kind") or "")
    base = _command_name(rec.command)
    if base in _PATH_RUNNERS:
        return rec.command, rec.args
    if kind in {"pip", "uv"}:
        vbin = _venv_bin(rec.mcp_id)
        if base in _PYTHON_RUNNERS:
            return f"{vbin}/python", rec.args
        if base and "/" not in str(rec.command):
            return f"{vbin}/{base}", rec.args
    if kind == "npm" and base and "/" not in str(rec.command):
        return f"{dest}/node_modules/.bin/{base}", rec.args
    return rec.command, rec.args


def _install_digest(rec, install: dict | None) -> str:
    payload = json.dumps(
        {"install": install, "files": rec.files},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _marker_current(session, marker: str, digest: str) -> bool:
    probe = session.exec(
        ["sh", "-c", f"cat {shlex.quote(marker)} 2>/dev/null"],
        timeout=15,
    )
    return bool(probe.ok and digest == (probe.stdout or "").strip())


def _python_imports_mcp(session, python: str) -> bool:
    if not session.exec(["test", "-x", python], timeout=15).ok:
        return False
    return session.exec([python, "-c", _MCP_IMPORT], timeout=30).ok


def _install_python(session, rec, dest: str, install: dict) -> str:
    """Install a Python MCP into an isolated venv (uv when available)."""
    manifest = str(install.get("manifest") or "")
    digest = _install_digest(rec, install)
    lock = f"/tmp/orchestrator-mcp/install-{rec.mcp_id}.lock"
    venv = f"/workspace/.peon/mcp-venvs/{rec.mcp_id}"
    python = f"{venv}/bin/python"
    marker = f"{venv}/.peon-ready"
    if session.exec(["test", "-x", python], timeout=15).ok and _marker_current(
        session, marker, digest
    ):
        return ""
    use_uv = bool(session.which("uv"))
    files = rec.files or {}
    manifest = manifest or ("pyproject.toml" if "pyproject.toml" in files else "")
    if manifest.endswith("pyproject.toml"):
        # A project with a build backend installs as a package (console scripts);
        # a plain dependency list (no [build-system]) cannot be built editable.
        buildable = "[build-system]" in str(files.get(manifest) or "")
        source = (
            f"-e {shlex.quote(dest)}" if buildable else f"-r {shlex.quote(f'{dest}/{manifest}')}"
        )
    elif manifest:
        source = f"-r {shlex.quote(f'{dest}/{manifest}')}"
    else:
        source = f"-e {shlex.quote(dest)}"
    if use_uv:
        # ``--allow-existing``: re-install into the same venv when the digest
        # changes (newer uv refuses to overwrite an existing directory).
        create = f"uv venv --allow-existing {shlex.quote(venv)}"
        install_cmd = f"uv pip install --python {shlex.quote(python)} {source}"
    else:
        create = f"python3 -m venv {shlex.quote(venv)}"
        install_cmd = f"{shlex.quote(python)} -m pip install --disable-pip-version-check {source}"
    script = "\n".join(
        [
            "set -e",
            "mkdir -p /tmp/orchestrator-mcp",
            f"exec 8>{shlex.quote(lock)}",
            "flock -w 600 8",
            f"if [ -x {shlex.quote(python)} ] && "
            f'[ "$(cat {shlex.quote(marker)} 2>/dev/null || true)" = '
            f"{shlex.quote(digest)} ]; then exit 0; fi",
            create,
            install_cmd,
            f"printf %s {shlex.quote(digest)} > {shlex.quote(marker)}",
        ]
    )
    installed = session.exec(["sh", "-c", script], cwd=dest, timeout=1200)
    if not installed.ok:
        tool = "uv" if use_uv else "pip"
        detail = (installed.stderr or installed.stdout or f"{tool} failed").strip()
        return f"Error: MCP dependency install failed: {detail[:800]}"
    return ""


def _install_npm(session, rec, dest: str, install: dict) -> str:
    digest = _install_digest(rec, install)
    lock = f"/tmp/orchestrator-mcp/install-{rec.mcp_id}.lock"
    if not session.which("npm"):
        return "Error: npm is not available in the sandbox image."
    marker = f"{dest}/.peon-npm-ready"
    if _marker_current(session, marker, digest):
        return ""
    script = "\n".join(
        [
            "set -e",
            "mkdir -p /tmp/orchestrator-mcp",
            f"exec 8>{shlex.quote(lock)}",
            "flock -w 600 8",
            f'if [ "$(cat {shlex.quote(marker)} 2>/dev/null || true)" = '
            f"{shlex.quote(digest)} ]; then exit 0; fi",
            "npm install --ignore-scripts --no-audit --no-fund",
            f"printf %s {shlex.quote(digest)} > {shlex.quote(marker)}",
        ]
    )
    installed = session.exec(["sh", "-c", script], cwd=dest, timeout=1200)
    if not installed.ok:
        detail = (installed.stderr or installed.stdout or "npm failed").strip()
        return f"Error: MCP dependency install failed: {detail[:800]}"
    return ""


def _post_install_commands(rec, install: dict | None) -> tuple[str, ...]:
    """One-off setup an MCP declares itself (``install.post`` in its manifest).

    The platform has no knowledge of any particular package: what a server
    needs beyond its dependency install is the server's to declare.
    """
    return tuple((install or {}).get("post") or ())


def _run_post_install(session, rec, dest: str, install: dict | None) -> None:
    """Best-effort one-off setup in the MCP's own environment (never fatal).

    Runs once per (manifests + commands) digest. A failure is logged: the MCP
    still starts, and the affected feature reports its own error.
    """
    commands = _post_install_commands(rec, install)
    if not commands:
        return
    files = rec.files or {}
    digest = hashlib.sha256(
        "\n".join(
            [str(files.get(n) or "") for n in ("pyproject.toml", "requirements.txt")]
            + list(commands)
        ).encode("utf-8")
    ).hexdigest()
    marker = f"{dest}/.peon-post-install"
    if _marker_current(session, marker, digest):
        return
    venv_bin = f"/workspace/.peon/mcp-venvs/{rec.mcp_id}/bin"
    runner = (
        f'if [ -x {shlex.quote(venv_bin)}/python ]; then PATH={shlex.quote(venv_bin)}:"$PATH"; '
        f'run() {{ sh -c "$1"; }}; '
        f"elif command -v uv >/dev/null 2>&1; then "
        f'run() {{ uv run --project {shlex.quote(dest)} sh -c "$1"; }}; '
        f'else run() {{ sh -c "$1"; }}; fi'
    )
    script = "\n".join(
        [
            "set -e",
            "mkdir -p /tmp/orchestrator-mcp",
            f"exec 8>/tmp/orchestrator-mcp/post-install-{rec.mcp_id}.lock",
            "flock -w 900 8",
            runner,
            *(f"run {shlex.quote(c)}" for c in commands),
            f"printf %s {shlex.quote(digest)} > {shlex.quote(marker)}",
        ]
    )
    done = session.exec(["sh", "-c", script], cwd=dest, timeout=1500)
    if not done.ok:
        logger.warning(
            "post-install failed for %s: %s",
            rec.mcp_id,
            (done.stderr or done.stdout or "").strip()[:400],
        )


def _install(session, rec, dest: str, install: dict | None) -> str:
    error = _install_deps(session, rec, dest, install)
    if not error:
        _run_post_install(session, rec, dest, install)
    return error


def _install_deps(session, rec, dest: str, install: dict | None) -> str:
    install = install or {}
    kind = str(install.get("kind") or "")
    if kind not in _INSTALL_KINDS:
        return ""
    if _command_name(rec.command) in {"uv", "uvx"}:
        # uv/uvx resolve and sync their own environment on every invocation
        # (``uv run --project .`` / ``uvx``). ``_runtime_command`` leaves the
        # command on PATH for these runners, so a side venv here would never
        # be used — installing into it only adds latency and a failure point
        # that has nothing to do with how the server actually launches.
        return ""
    manifest = str(install.get("manifest") or "")
    if manifest and manifest not in (rec.files or {}):
        return f"Error: MCP install manifest {manifest!r} is not in the server files."
    if kind in {"pip", "uv"}:
        return _install_python(session, rec, dest, install)
    return _install_npm(session, rec, dest, install)


@lru_cache(maxsize=1)
def _package_supervisor_text() -> str:
    return _PACKAGE_SUPERVISOR.read_text(encoding="utf-8")


def _supervisor_content_digest() -> str:
    return hashlib.sha256(_package_supervisor_text().encode("utf-8")).hexdigest()


def _supervisor_stamp() -> str:
    return hashlib.sha256(
        (_package_supervisor_text() + "\n" + _MCP_SDK_SPEC).encode("utf-8")
    ).hexdigest()


def _supervisor_alive(session, stamp: str) -> bool:
    script = (
        f"pid=$(cat {shlex.quote(_PID)} 2>/dev/null || true); "
        f"have=$(cat {shlex.quote(_STAMP)} 2>/dev/null || true); "
        f'if [ -n "$pid" ] && [ "$have" = {shlex.quote(stamp)} ] '
        f'&& [ -S {shlex.quote(_SOCK)} ] && kill -0 "$pid" 2>/dev/null; then '
        "echo up; else echo down; fi"
    )
    probe = session.exec(["sh", "-c", script], timeout=15)
    return (probe.stdout or "").strip() == "up"


def _resolve_mcp_python(session) -> str | None:
    if _python_imports_mcp(session, _IMAGE_PYTHON):
        return _IMAGE_PYTHON
    digest = hashlib.sha256(_MCP_SDK_SPEC.encode("utf-8")).hexdigest()
    if _marker_current(session, _FALLBACK_MARKER, digest) and _python_imports_mcp(
        session, _FALLBACK_PYTHON
    ):
        return _FALLBACK_PYTHON
    return None


def _ensure_mcp_sdk(session) -> tuple[str, str]:
    """Ensure the official ``mcp`` SDK is importable. Returns ``(error, python)``."""
    existing = _resolve_mcp_python(session)
    if existing:
        return "", existing
    digest = hashlib.sha256(_MCP_SDK_SPEC.encode("utf-8")).hexdigest()
    lock = "/tmp/orchestrator-mcp/sdk-install.lock"
    script = "\n".join(
        [
            "set -e",
            "mkdir -p /tmp/orchestrator-mcp /workspace/.peon/mcp-venvs",
            f"exec 8>{shlex.quote(lock)}",
            "flock -w 600 8",
            f"if [ -x {shlex.quote(_IMAGE_PYTHON)} ] && "
            f"{shlex.quote(_IMAGE_PYTHON)} -c {shlex.quote(_MCP_IMPORT)}; then exit 0; fi",
            f"if [ -x {shlex.quote(_FALLBACK_PYTHON)} ] && "
            f'[ "$(cat {shlex.quote(_FALLBACK_MARKER)} 2>/dev/null || true)" = '
            f"{shlex.quote(digest)} ] && "
            f"{shlex.quote(_FALLBACK_PYTHON)} -c {shlex.quote(_MCP_IMPORT)}; then exit 0; fi",
            f"python3 -m venv {shlex.quote(_FALLBACK_VENV)}",
            f"{shlex.quote(_FALLBACK_PYTHON)} -m pip install "
            "--disable-pip-version-check "
            f"{shlex.quote(_MCP_SDK_SPEC)}",
            f"printf %s {shlex.quote(digest)} > {shlex.quote(_FALLBACK_MARKER)}",
        ]
    )
    installed = session.exec(["sh", "-c", script], timeout=1200)
    if not installed.ok:
        detail = (installed.stderr or installed.stdout or "mcp SDK install failed").strip()
        return (
            f"Error: could not install the official mcp SDK in the sandbox: {detail[:800]}",
            "",
        )
    python = _resolve_mcp_python(session)
    if not python:
        return "Error: mcp SDK is installed but not importable in the sandbox.", ""
    return "", python


def _resolve_supervisor_script(session) -> tuple[str, str]:
    """Prefer image-baked supervisor when it matches the package file."""
    want = _supervisor_content_digest()
    if session.exec(["test", "-f", _IMAGE_SUPERVISOR], timeout=15).ok:
        probe = session.exec(["sha256sum", _IMAGE_SUPERVISOR], timeout=15)
        have = (probe.stdout or "").split()[0] if probe.ok else ""
        if have == want:
            return "", _IMAGE_SUPERVISOR
    wrote = materialize_tree(
        session,
        "/workspace/.peon/mcp",
        {"supervisor.py": _package_supervisor_text()},
    )
    if wrote:
        return wrote, ""
    return "", _WORKSPACE_SUPERVISOR


def _ensure_supervisor(session) -> str:
    stamp = _supervisor_stamp()
    if _supervisor_alive(session, stamp):
        return ""
    sdk_error, python = _ensure_mcp_sdk(session)
    if sdk_error:
        return sdk_error
    path_error, script = _resolve_supervisor_script(session)
    if path_error:
        return path_error
    session.exec(
        [
            "sh",
            "-c",
            f"mkdir -p {shlex.quote(_ARTIFACT_DIR)} /workspace/workspace",
        ],
        timeout=15,
    )
    launch = "\n".join(
        [
            f"mkdir -p /tmp/orchestrator-mcp /workspace/.peon/mcp {shlex.quote(_ARTIFACT_DIR)}",
            "exec 9>/tmp/orchestrator-mcp/supervisor.lock",
            "flock -w 20 9 || exit 1",
            f"pid=$(cat {shlex.quote(_PID)} 2>/dev/null || true)",
            f"have=$(cat {shlex.quote(_STAMP)} 2>/dev/null || true)",
            f'if [ -n "$pid" ] && [ "$have" = {shlex.quote(stamp)} ] '
            f'&& [ -S {shlex.quote(_SOCK)} ] && kill -0 "$pid" 2>/dev/null; then',
            "  exit 0",
            "fi",
            'if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; sleep 0.2; fi',
            f"rm -f {shlex.quote(_SOCK)} {shlex.quote(_PID)}",
            f"printf %s {shlex.quote(stamp)} > {shlex.quote(_STAMP)}",
            f"setsid nohup {shlex.quote(python)} {shlex.quote(script)} "
            ">/tmp/orchestrator-mcp/supervisor.log 2>&1 < /dev/null &",
        ]
    )
    launched = session.exec(["sh", "-c", launch], timeout=30)
    if not launched.ok:
        detail = (launched.stderr or launched.stdout or "supervisor launch failed").strip()
        return f"Error: could not start the MCP supervisor in the sandbox: {detail[:400]}"
    for _ in range(40):
        if _supervisor_alive(session, stamp):
            return ""
        session.exec(["sleep", "0.25"], timeout=5)
    log = session.exec(["sh", "-c", "tail -n 60 /tmp/orchestrator-mcp/supervisor.log"], timeout=15)
    detail = (log.stdout or log.stderr or "supervisor socket did not appear").strip()
    return f"Error: MCP supervisor did not start: {detail[:500]}"


def _sock_rpc(session, request: dict) -> dict:
    try:
        limit = float(request.get("timeout") or 90)
    except (TypeError, ValueError):
        limit = 90
    # The first call may start the server (``uv run`` sync, ``npx`` fetch).
    wait = int(limit) + _STARTUP_SECONDS + 10
    result = session.exec(
        ["python3", "-c", _SOCK_RPC, _SOCK, str(wait)],
        stdin=json.dumps(request),
        timeout=wait + 15,
    )
    raw = (result.stdout or "").strip()
    if not raw:
        detail = (result.stderr or "empty supervisor response").strip()
        return {"ok": False, "error": detail[:500]}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": raw[:500]}
    if not isinstance(data, dict):
        return {"ok": False, "error": "supervisor returned a non-object"}
    return data
