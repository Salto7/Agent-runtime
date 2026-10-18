"""tools/catalog: load YAML definitions and provision CLIs before skill runs."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Iterable

import yaml

from orchestrator.sandbox import SandboxSession
from orchestrator.utils.service import SharedService

logger = logging.getLogger(__name__)

_REPO_RE = re.compile(
    r"^(?:https?://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_TRANSITIVE: dict[str, list[str]] = {}
_GH_SCRIPT = Path(__file__).with_name("assets") / "install_github_release.sh"


def normalize_verify(raw: Any) -> list[dict[str, Any]]:
    """Coerce verify to a list of ``{command: …}`` steps.

    LLMs often emit ``verify: sqlmap -h`` (a string). ``list("sqlmap -h")`` would
    become character crumbs and silently fail every check — normalize instead.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        cmd = raw.strip()
        return [{"command": cmd}] if cmd else []
    if isinstance(raw, dict):
        cmd = str(raw.get("command") or "").strip()
        return [dict(raw)] if cmd else []
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append({"command": item.strip()})
            elif isinstance(item, dict) and str(item.get("command") or "").strip():
                out.append(dict(item))
        return out
    return []


@dataclass
class CatalogTool:
    id: str
    name: str
    description: str = ""
    tier: str = "catalog"
    binary: str = ""
    binaries: list[str] = field(default_factory=list)
    install: list[dict[str, Any]] = field(default_factory=list)
    verify: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def is_image_tier(self) -> bool:
        return (self.tier or "").strip().lower() == "image"

    def provides(self, name: str) -> bool:
        key = (name or "").strip().lower()
        if not key:
            return False
        if key == (self.id or "").strip().lower():
            return True
        if key == (self.binary or "").strip().lower():
            return True
        return key in {b.strip().lower() for b in (self.binaries or []) if b.strip()}


@dataclass
class ProvisionResult:
    cli_ids: list[str] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "cli_ids": self.cli_ids,
            "installed": self.installed,
            "verified": self.verified,
            "errors": self.errors,
            "ok": self.ok,
        }


def _run(
    cmd: list[str] | str, *, timeout: int = 300, shell: bool = False
) -> tuple[int, str, str]:
    res = SandboxSession.current().exec(cmd, timeout=timeout, shell=shell)
    return res.code, res.stdout, res.stderr


def _pkgs(raw: dict[str, Any]) -> list[str]:
    return [str(p).strip() for p in (raw.get("packages") or []) if str(p).strip()]


# apt may install python3/nodejs while upstream scripts use `#!/usr/bin/env python|node`.
_APT_ENV_ALIASES: tuple[tuple[str, str], ...] = (
    ("python", "python3"),
    ("node", "nodejs"),
)


def _ensure_env_command_aliases(installed_packages: Iterable[str]) -> None:
    pkgs = {p.strip().lower() for p in installed_packages}
    for alias, target in _APT_ENV_ALIASES:
        if target not in pkgs:
            continue
        _run(
            [
                "bash",
                "-lc",
                f"command -v {alias} >/dev/null 2>&1 || "
                f"{{ command -v {target} >/dev/null 2>&1 && "
                f'ln -sfn "$(command -v {target})" "/usr/local/bin/{alias}"; }}',
            ],
            timeout=30,
        )


class InstallStep(ABC):
    """One catalog install action (custom / apt / github_release / pip / git_clone)."""

    _registry: ClassVar[dict[str, type[InstallStep]]] = {}
    _TYPE_ALIASES: ClassVar[dict[str, str]] = {
        "command": "custom",
        "shell": "custom",
        "run": "custom",
        "bash": "custom",
        "script": "custom",
    }

    def __init_subclass__(cls, *, step_type: str = "", **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if step_type:
            InstallStep._registry[step_type] = cls

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> InstallStep | None:
        stype = str(raw.get("type") or "").strip().lower()
        if not stype and str(raw.get("command") or "").strip():
            stype = "custom"
        stype = cls._TYPE_ALIASES.get(stype, stype)
        impl = cls._registry.get(stype)
        return impl(raw) if impl else None  # type: ignore[call-arg]

    @abstractmethod
    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]: ...


def _under_catalog(catalog_dir: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(catalog_dir.resolve())
        return True
    except (OSError, ValueError):
        return False


class CustomInstallStep(InstallStep, step_type="custom"):
    """Priority install — runs before apt / github_release / pip / git_clone.

    YAML::

        install:
          - type: custom
            command: "curl -fsSL https://example/install.sh | bash"
          - type: apt
            packages: [fallback-pkg]

        # Or reference the tool's install script (same stem as the YAML):
        # tools/catalog/mytool.yaml + tools/catalog/mytool.sh
          - type: custom
            command: mytool.sh

    ``command`` is either an inline shell line, or a relative path to the single
    allowed ``{tool_id}.sh`` beside ``{tool_id}.yaml`` (skill-style relative path).
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self.command = str(raw.get("command") or raw.get("run") or "").strip()
        try:
            self.timeout = max(30, int(raw.get("timeout") or 600))
        except (TypeError, ValueError):
            self.timeout = 600

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del binary
        tid = (tool_id or "").strip()
        catalog_dir = ToolCatalog.shared().catalog_dir()
        payload, label = self._resolve_payload(tid, catalog_dir)
        if not payload:
            return False, label or "empty custom install"
        code, out, err = _run(["bash", "-lc", payload], timeout=self.timeout)
        if code:
            detail = (err or out).strip()[:500]
            return False, f"custom install: {detail or f'exit {code}'}"
        return True, label

    def _resolve_payload(self, tool_id: str, catalog_dir: Path) -> tuple[str, str]:
        """Inline shell, or the tool's ``{id}.sh`` under the catalog dir."""
        raw = self.command
        script_name = f"{tool_id}.sh" if tool_id else ""
        script_path = (catalog_dir / script_name).resolve() if script_name else None

        def _is_script_ref(text: str) -> bool:
            if not script_name:
                return False
            norm = text.replace("\\", "/").lstrip("./")
            return (
                norm == script_name
                or norm.endswith(f"/{script_name}")
                or Path(norm).name == script_name
            )

        if script_path is not None and (
            not raw or _is_script_ref(raw)
        ):
            if script_path.is_file() and _under_catalog(catalog_dir, script_path):
                try:
                    body = script_path.read_text(encoding="utf-8")
                except OSError as exc:
                    return "", f"cannot read {script_name}: {exc}"
                if not body.strip():
                    return "", f"empty install script {script_name}"
                return body, f"custom:script:{script_name}"
            if raw:
                return "", f"missing install script {script_name} (beside {tool_id}.yaml)"
            return "", f"empty custom install (no command and no {tool_id}.sh)"

        if not raw:
            return "", "empty custom install"
        if raw.endswith(".sh") and ("/" in raw or "\\" in raw or Path(raw).name != raw):
            # Disallow other script paths — only {tool_id}.sh is allowed.
            return (
                "",
                f"custom script must be {script_name or '{tool_id}.sh'} beside the tool YAML",
            )
        return raw, "custom:inline"


class AptInstallStep(InstallStep, step_type="apt"):
    def __init__(self, raw: dict[str, Any]) -> None:
        self.packages = _pkgs(raw)

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del binary, tool_id
        if not self.packages:
            return True, "no apt packages"
        _run(["apt-get", "update", "-qq"], timeout=180)
        code, out, err = _run(
            ["apt-get", "install", "-y", "--no-install-recommends", *self.packages],
            timeout=600,
        )
        if code:
            return False, f"apt install {self.packages}: {(err or out).strip()[:400]}"
        _ensure_env_command_aliases(self.packages)
        return True, f"apt:{','.join(self.packages)}"


class GitHubReleaseInstallStep(InstallStep, step_type="github_release"):
    def __init__(self, raw: dict[str, Any]) -> None:
        self.repo = str(raw.get("repo") or "").strip()
        self.binary = str(raw.get("binary") or "").strip()
        self.asset_substr = str(raw.get("asset_substr") or "linux_amd64").strip()
        self.tag = str(raw.get("tag") or "").strip()

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del tool_id
        match = _REPO_RE.match(self.repo)
        if not match:
            return False, f"invalid github repo {self.repo!r}"
        owner, name = match.group(1), match.group(2)
        bin_name = (self.binary or binary or name).strip()
        if not bin_name:
            return False, "binary name required"
        if not _GH_SCRIPT.is_file():
            return False, f"missing install script {_GH_SCRIPT}"
        script = _GH_SCRIPT.read_text(encoding="utf-8")
        for key, val in {
            "__OWNER__": shlex.quote(owner),
            "__REPO__": shlex.quote(name),
            "__BINARY__": shlex.quote(bin_name),
            "__TAG__": shlex.quote(self.tag),
            "__ASSET_SUBSTR__": shlex.quote(self.asset_substr or "linux_amd64"),
        }.items():
            script = script.replace(key, val)
        code, out, err = _run(["bash", "-lc", script], timeout=600)
        if code:
            return False, f"github {self.repo}/{bin_name}: {(err or out).strip()[:500]}"
        return True, f"github:{bin_name}"


class PipInstallStep(InstallStep, step_type="pip"):
    def __init__(self, raw: dict[str, Any]) -> None:
        self.packages = _pkgs(raw)

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del binary, tool_id
        if not self.packages:
            return True, "no pip packages"
        code, out, err = _run(
            ["python3", "-m", "pip", "install", "--break-system-packages", *self.packages],
            timeout=600,
        )
        if code:
            return False, f"pip {self.packages}: {(err or out).strip()[:300]}"
        return True, f"pip:{' '.join(self.packages)}"


class GitCloneInstallStep(InstallStep, step_type="git_clone"):
    """Shallow-clone a GitHub repo and symlink an entrypoint onto PATH."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.repo = str(raw.get("repo") or "").strip()
        self.ref = str(raw.get("ref") or "").strip()
        self.entrypoint = str(raw.get("entrypoint") or "").strip()
        self.binary = str(raw.get("binary") or "").strip()
        try:
            self.depth = max(1, int(raw.get("depth") or 1))
        except (TypeError, ValueError):
            self.depth = 1

    def apply(self, *, binary: str = "", tool_id: str = "") -> tuple[bool, str]:
        del tool_id
        match = _REPO_RE.match(self.repo)
        if not match:
            return False, f"invalid github repo {self.repo!r}"
        owner, name = match.group(1), match.group(2)
        entry = (self.entrypoint or "").strip().lstrip("/")
        if not entry or entry.startswith("..") or "/../" in f"/{entry}/":
            return False, "entrypoint required (repo-relative path)"
        bin_name = (self.binary or binary or Path(entry).stem).strip()
        if not bin_name:
            return False, "binary name required"
        dest = Path("/opt/catalog-tools") / name
        url = f"https://github.com/{owner}/{name}.git"
        entry_path = dest / entry
        bin_path = f"/usr/local/bin/{bin_name}"
        AptInstallStep({"packages": ["git"]}).apply()
        script = f"""
set -euo pipefail
mkdir -p /opt/catalog-tools
rm -rf {shlex.quote(str(dest))}
git clone --depth {self.depth} {shlex.quote(url)} {shlex.quote(str(dest))}
"""
        if self.ref:
            script += f"git -C {shlex.quote(str(dest))} fetch --depth {self.depth} origin {shlex.quote(self.ref)}\n"
            script += f"git -C {shlex.quote(str(dest))} checkout {shlex.quote(self.ref)}\n"
        script += f"""
ENTRY={shlex.quote(str(entry_path))}
test -f "$ENTRY"
chmod +x "$ENTRY" || true
ln -sfn "$ENTRY" {shlex.quote(bin_path)}
"""
        code, out, err = _run(["bash", "-lc", script], timeout=600)
        if code:
            return False, f"git_clone {self.repo}: {(err or out).strip()[:500]}"
        return True, f"git_clone:{bin_name}"


def _steps(raw_steps: list[Any]) -> list[InstallStep]:
    out: list[InstallStep] = []
    for raw in raw_steps or []:
        if isinstance(raw, dict):
            step = InstallStep.from_dict(raw)
            if step:
                out.append(step)
    return out


def _fallback_rank(step: InstallStep) -> int:
    """After custom: apt (batched) → github_release → pip → git_clone."""
    if isinstance(step, GitHubReleaseInstallStep):
        return 0
    if isinstance(step, PipInstallStep):
        return 1
    if isinstance(step, GitCloneInstallStep):
        return 2
    return 9


class ToolCatalog(SharedService):
    """Cached view of ``tools/catalog/*.yaml``."""

    def __init__(self) -> None:
        self._cache: dict[str, CatalogTool] | None = None

    def catalog_dir(self) -> Path:
        raw = (os.environ.get("TOOLS_CATALOG_DIR") or "").strip()
        if raw:
            return Path(raw).resolve()
        from orchestrator.config import get_config

        return Path(get_config().tools_catalog_dir).resolve()

    def invalidate(self) -> None:
        self._cache = None

    @staticmethod
    def _fingerprint(tool: CatalogTool) -> tuple:
        return (
            tool.name,
            tool.description,
            tool.tier,
            tool.binary,
            tuple(tool.binaries or []),
            tuple(tool.skills or []),
            tuple(tool.tags or []),
        )

    def reload_tools(self) -> dict:
        """Force-rescan ``tools/catalog`` YAML (same path as ``invalidate`` + ``all``)."""
        # Snapshot the in-memory cache only — do not reload first, or a
        # just-edited YAML would look unchanged in the diff.
        if self._cache is not None:
            before = {tid: self._fingerprint(t) for tid, t in self._cache.items()}
            before_desc = {tid: t.description for tid, t in self._cache.items()}
        else:
            current = self.all()
            before = {tid: self._fingerprint(t) for tid, t in current.items()}
            before_desc = {tid: t.description for tid, t in current.items()}
        self.invalidate()
        after = self.all()

        diff: dict[str, list[dict]] = {"added": [], "removed": [], "modified": []}
        for tid in sorted(set(after) - set(before)):
            diff["added"].append({"id": tid, "description": after[tid].description})
        for tid in sorted(set(before) - set(after)):
            diff["removed"].append({"id": tid, "description": before_desc[tid]})
        for tid in sorted(set(before) & set(after)):
            if before[tid] != self._fingerprint(after[tid]):
                diff["modified"].append(
                    {"id": tid, "description": after[tid].description}
                )
        return diff

    def all(self) -> dict[str, CatalogTool]:
        if self._cache is not None:
            return self._cache
        by_id: dict[str, CatalogTool] = {}
        root = self.catalog_dir()
        if root.is_dir():
            for path in sorted(root.glob("*.yaml")):
                tool = self._load(path)
                if tool:
                    by_id[tool.id] = tool
        self._cache = by_id
        return by_id

    def by_id(self, tool_id: str) -> CatalogTool | None:
        key = (tool_id or "").strip().lower()
        return self.all().get(key) if key else None

    def by_binary(self, binary: str) -> CatalogTool | None:
        name = (binary or "").strip().lower()
        if not name:
            return None
        catalog = self.all()
        if name in catalog:
            return catalog[name]
        return next((t for t in catalog.values() if t.provides(name)), None)

    def lookup(self, key: str) -> CatalogTool | None:
        """Resolve by binary name or catalog id."""
        return self.by_binary(key) or self.by_id(key)

    def summaries(self, *, limit: int | None = 80) -> list[dict[str, str]]:
        """Non-image catalog tools as compact dicts (authoring / LLM prompts)."""
        out: list[dict[str, str]] = []
        for tool in sorted(self.all().values(), key=lambda t: t.id):
            if tool.is_image_tier:
                continue
            out.append(
                {
                    "id": tool.id,
                    "binary": (tool.binary or tool.id).strip(),
                    "description": (tool.description or "")[:240],
                    "install_types": ",".join(
                        str(s.get("type") or "")
                        for s in (tool.install or [])
                        if isinstance(s, dict)
                    ),
                }
            )
            if limit is not None and len(out) >= limit:
                break
        return out

    def for_skills(self, skill_names: list[str] | set[str]) -> list[CatalogTool]:
        catalog = self.all()
        expanded: list[str] = []
        seen: set[str] = set()

        def add(raw: str) -> None:
            name = (raw or "").strip()
            if not name or name in seen:
                return
            seen.add(name)
            expanded.append(name)
            for dep in _TRANSITIVE.get(name, []):
                add(dep)

        for raw in skill_names or []:
            add(str(raw))

        tool_ids: list[str] = []
        for skill_name in expanded:
            for tid in self._toolkit_tool_ids(skill_name):
                if tid not in tool_ids:
                    tool_ids.append(tid)

        out: list[CatalogTool] = []
        for tid in tool_ids:
            entry = catalog.get(tid)
            if entry is None:
                logger.warning("Unknown catalog tool %r for skills %s", tid, expanded)
                continue
            if not entry.is_image_tier:
                out.append(entry)
        return out

    def _toolkit_tool_ids(self, skill_name: str) -> list[str]:
        # deferred: SkillRegistry → skills.execute → runtime_shell → runtime → catalog
        from orchestrator.skills.misc.registry import SkillRegistry

        skill = SkillRegistry.shared().load_skill((skill_name or "").strip())
        if skill is None:
            return []
        out: list[str] = []
        for item in skill.toolkit or []:
            key = str(item).strip().lower()
            if not key:
                continue
            tool = self.lookup(key)
            if tool is None:
                logger.warning("No catalog tool for skill %s CLI %r", skill_name, key)
                continue
            if tool.id not in out:
                out.append(tool.id)
        return out

    @staticmethod
    def _load(path: Path) -> CatalogTool | None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Failed to load catalog tool %s: %s", path, exc)
            return None
        if not isinstance(raw, dict):
            return None
        cid = str(raw.get("id") or path.stem).strip()
        if not cid or cid.startswith("_"):
            return None
        binary = str(raw.get("binary") or raw.get("name") or cid).strip()
        binaries = [
            str(b).strip()
            for b in (raw.get("binaries") or [])
            if str(b).strip()
        ]
        if binary and binary not in binaries:
            binaries = [binary, *binaries]
        return CatalogTool(
            id=cid,
            name=str(raw.get("name") or cid).strip(),
            description=str(raw.get("description") or "").strip(),
            tier=str(raw.get("tier") or "catalog").strip(),
            binary=binary,
            binaries=binaries,
            install=list(raw.get("install") or [])
            if isinstance(raw.get("install"), list)
            else [],
            verify=normalize_verify(raw.get("verify")),
            skills=[str(s).strip() for s in (raw.get("skills") or []) if str(s).strip()],
            tags=[str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()],
        )


class CatalogProvisioner(SharedService):
    """Verify/install catalog CLIs for skills (host or worker container)."""

    def __init__(self, *, catalog: ToolCatalog | None = None) -> None:
        self._catalog = catalog or ToolCatalog.shared()

    def provision(
        self, skill_names: list[str], *, workspace: Path | None = None
    ) -> ProvisionResult:
        result = ProvisionResult()
        tools = self._catalog.for_skills(skill_names)
        result.cli_ids = [t.id for t in tools]
        needed = [t for t in tools if not self._verified(t)]
        if needed:
            # Priority: custom → apt → github_release → pip / git_clone.
            for tool in needed:
                self._apply_custom_steps(tool, result)
            remaining = [t for t in needed if not self._verified(t)]
            if remaining:
                self._apt_batch(remaining, result)
                for tool in remaining:
                    self._provision_tool(tool, result, skip_apt=True)
        result.verified.extend(t.id for t in tools if self._verified(t))
        # de-dupe verified while preserving order
        result.verified = list(dict.fromkeys(result.verified))
        self._write_manifest(workspace, skill_names, result)
        return result

    def provision_binary(self, binary: str) -> tuple[bool, str]:
        tool = self._catalog.by_binary(binary)
        if tool is None:
            return False, f"no catalog entry for {binary!r}"
        if self._verified(tool):
            return True, f"{tool.binary or tool.id} verified"
        result = ProvisionResult()
        self._apply_custom_steps(tool, result)
        if self._verified(tool):
            return True, f"installed {tool.id}"
        self._provision_tool(tool, result, skip_apt=False)
        if self._verified(tool):
            return True, f"installed {tool.id}"
        return False, "; ".join(result.errors) or "verify failed"

    def _apply_custom_steps(self, tool: CatalogTool, result: ProvisionResult) -> bool:
        """Run ``type: custom`` install steps first. Soft-fail → fall through."""
        if self._verified(tool):
            return True
        binary = tool.binary or tool.id
        ran = False
        for step in _steps(tool.install):
            if not isinstance(step, CustomInstallStep):
                continue
            ran = True
            ok, msg = step.apply(binary=binary, tool_id=tool.id)
            if ok:
                if msg and msg not in result.installed:
                    result.installed.append(msg)
                if self._verified(tool):
                    return True
            else:
                logger.info(
                    "priority custom install for %s did not verify (%s); falling back",
                    tool.id,
                    msg,
                )
        if ran and self._verified(tool):
            return True
        return False

    def _provision_tool(
        self, tool: CatalogTool, result: ProvisionResult, *, skip_apt: bool
    ) -> None:
        if self._verified(tool):
            return
        steps = [
            s for s in _steps(tool.install) if not isinstance(s, CustomInstallStep)
        ]
        if not steps and not any(
            isinstance(s, CustomInstallStep) for s in _steps(tool.install)
        ):
            result.errors.append(f"{tool.id}: no install steps in catalog")
            return
        if not steps:
            if not any(e.startswith(f"{tool.id}:") for e in result.errors):
                result.errors.append(f"{tool.id}: verify failed after install")
            return
        if not skip_apt:
            self._apt_batch([tool], result)
        binary = tool.binary or tool.id
        # After apt batch: github_release, then pip, then git_clone.
        for step in sorted(
            (s for s in steps if not isinstance(s, AptInstallStep)),
            key=_fallback_rank,
        ):
            ok, msg = step.apply(binary=binary, tool_id=tool.id)
            if not ok:
                result.errors.append(f"{tool.id}: {msg}")
                return
            if msg and msg not in result.installed:
                result.installed.append(msg)
        if not self._verified(tool) and not any(
            e.startswith(f"{tool.id}:") for e in result.errors
        ):
            result.errors.append(f"{tool.id}: verify failed after install")

    @staticmethod
    def _verified(tool: CatalogTool) -> bool:
        return bool(CatalogProvisioner.run_verify(tool).get("ok"))

    @staticmethod
    def run_verify(tool: CatalogTool) -> dict[str, Any]:
        """Run verify commands and return structured output for Learn / logs."""
        steps_out: list[dict[str, Any]] = []
        any_ok = False
        chunks: list[str] = []
        verify_steps = normalize_verify(tool.verify)
        if not verify_steps:
            return {
                "ok": False,
                "steps": [],
                "output": "(no verify commands in YAML — add verify: [{command: \"…\"}])",
            }
        for step in verify_steps:
            cmd = str(step.get("command") or "").strip()
            if not cmd:
                continue
            code, out, err = _run(cmd, timeout=120, shell=True)
            stdout = out or ""
            stderr = err or ""
            combined = f"{stdout}{stderr}"
            ok = code == 0
            must_match = str(step.get("must_match") or "").strip()
            if ok and must_match and not re.search(must_match, combined, re.I):
                ok = False
            must_not = str(step.get("must_not_match") or "").strip()
            if ok and must_not and re.search(must_not, combined, re.I):
                ok = False
            if ok:
                any_ok = True
            steps_out.append(
                {
                    "command": cmd,
                    "code": code,
                    "ok": ok,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            )
            chunks.append(f"$ {cmd}\nexit={code} ok={ok}")
            if stdout.strip():
                chunks.append(stdout.rstrip())
            if stderr.strip():
                chunks.append(stderr.rstrip())
            chunks.append("")
        return {
            "ok": any_ok,
            "steps": steps_out,
            "output": "\n".join(chunks).rstrip() or "(empty verify output)",
        }

    @staticmethod
    def _apt_batch(tools: list[CatalogTool], result: ProvisionResult) -> None:
        pkgs: list[str] = []
        for tool in tools:
            for step in _steps(tool.install):
                if isinstance(step, AptInstallStep):
                    for p in step.packages:
                        if p not in pkgs:
                            pkgs.append(p)
        if not pkgs:
            return
        ok, msg = AptInstallStep({"packages": pkgs}).apply()
        (result.installed if ok else result.errors).append(msg)

    @staticmethod
    def _write_manifest(
        workspace: Path | None, skill_names: list[str], result: ProvisionResult
    ) -> None:
        if workspace is None:
            return
        try:
            path = Path(workspace) / ".sandbox-cli-manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"skill_names": list(skill_names or []), **result.to_dict()}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Failed to write tool manifest under %s", workspace, exc_info=True)
