"""tools/catalog: load YAML definitions and provision CLIs before role runs."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from orchestrator.config import get_config
from orchestrator.tools.install_step_base import InstallStepBase
from orchestrator.tools.install_steps import (
    AptInstallStep,
    CustomInstallStep,
    _fallback_rank,
    _steps,
)
from orchestrator.tools.install_steps.util import _run
from orchestrator.utils.commands import clear_command_caches
from orchestrator.utils.service import SharedServiceBase

logger = logging.getLogger(__name__)


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
    roles: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # Other catalog tools this one needs (declared in its yaml ``requires:``).
    requires: list[str] = field(default_factory=list)

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


class ToolCatalog(SharedServiceBase):
    """Cached view of ``tools/catalog/*.yaml``."""

    def __init__(self, *, root: Path | None = None) -> None:
        self._cache: dict[str, CatalogTool] | None = None
        self._root = Path(root).resolve() if root is not None else None

    def catalog_dir(self) -> Path:
        if self._root is not None:
            return self._root
        raw = (os.environ.get("TOOLS_CATALOG_DIR") or "").strip()
        if raw:
            return Path(raw).resolve()
        return Path(get_config().tools_catalog_dir).resolve()

    def invalidate(self) -> None:
        try:
            clear_command_caches()
        except Exception as exc:
            logger.warning("clearing command caches failed: %s", exc, exc_info=True)

        self._cache = None

    @staticmethod
    def _fingerprint(tool: CatalogTool) -> tuple:
        return (
            tool.name,
            tool.description,
            tool.tier,
            tool.binary,
            tuple(tool.binaries or []),
            tuple(tool.tags or []),
        )

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

    def for_keys(self, keys: list[str] | set[str]) -> list[CatalogTool]:
        """Resolve catalog tools from CLI/binary/tool id keys."""
        catalog = self.all()
        expanded: list[str] = []
        seen: set[str] = set()

        def add(raw: str) -> None:
            name = (raw or "").strip()
            if not name or name in seen:
                return
            seen.add(name)
            expanded.append(name)
            tool = self.lookup(name)
            for dep in tool.requires if tool is not None else ():
                add(dep)

        for raw in keys or []:
            add(str(raw))

        tool_ids: list[str] = []
        for key in expanded:
            tool = self.lookup(key)
            if tool is None or tool.is_image_tier:
                continue
            if tool.id not in tool_ids:
                tool_ids.append(tool.id)

        out: list[CatalogTool] = []
        for tid in tool_ids:
            entry = catalog.get(tid)
            if entry is None:
                logger.warning("Unknown catalog tool %r for keys %s", tid, expanded)
                continue
            if not entry.is_image_tier:
                out.append(entry)
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
        binaries = [str(b).strip() for b in (raw.get("binaries") or []) if str(b).strip()]
        if binary and binary not in binaries:
            binaries = [binary, *binaries]
        return CatalogTool(
            id=cid,
            name=str(raw.get("name") or cid).strip(),
            description=str(raw.get("description") or "").strip(),
            tier=str(raw.get("tier") or "catalog").strip(),
            binary=binary,
            binaries=binaries,
            install=list(raw.get("install") or []) if isinstance(raw.get("install"), list) else [],
            verify=normalize_verify(raw.get("verify")),
            # Roles bind skills through inferred affinity, not tool YAML.
            roles=[],
            tags=[str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()],
            requires=[str(t).strip() for t in (raw.get("requires") or []) if str(t).strip()]
            if isinstance(raw.get("requires"), list)
            else [],
        )


class CatalogProvisioner(SharedServiceBase):
    """Verify/install catalog CLIs for role allowlists (host or worker container)."""

    def __init__(self, *, catalog: ToolCatalog | None = None) -> None:
        self._catalog = catalog or ToolCatalog.shared()

    def provision(self, keys: list[str], *, workspace: Path | None = None) -> ProvisionResult:
        result = ProvisionResult()
        tools = self._catalog.for_keys(keys)
        result.cli_ids = [t.id for t in tools]
        needed = [t for t in tools if not self._verified(t)]
        if needed:
            # Runtime order: custom (if any) runs first as a bootstrap attempt;
            # recipes should still prefer apt → github_release → git_clone → pip → custom.
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
        self._write_manifest(workspace, keys, result)
        return result

    def provision_binary(self, binary: str) -> tuple[bool, str]:
        tool = self._catalog.by_binary(binary)
        if tool is None:
            return False, f"no catalog entry for {binary!r}"
        if self._verified(tool):
            label = tool.binary or tool.id
            return True, f"{label} already installed"
        result = ProvisionResult()
        self._apply_custom_steps(tool, result)
        if self._verified(tool):
            return True, f"installed {tool.id}"
        self._provision_tool(tool, result, skip_apt=False)
        if self._verified(tool):
            return True, f"installed {tool.id}"
        return False, "; ".join(result.errors) or "verify failed"

    def _apply_step(self, step: InstallStepBase, *, binary: str, tool_id: str) -> tuple[bool, str]:
        """Run one step. Custom scripts resolve beside this provisioner's catalog."""
        if isinstance(step, CustomInstallStep):
            return step.apply(
                binary=binary,
                tool_id=tool_id,
                catalog_dir=self._catalog.catalog_dir(),
            )
        return step.apply(binary=binary, tool_id=tool_id)

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
            ok, msg = self._apply_step(step, binary=binary, tool_id=tool.id)
            if ok:
                if msg and msg not in result.installed:
                    result.installed.append(msg)
                if self._verified(tool):
                    return True
            else:
                result.errors.append(f"{tool.id}: {msg}")
                logger.info(
                    "priority custom install for %s did not verify (%s); falling back",
                    tool.id,
                    msg,
                )
        return bool(ran and self._verified(tool))

    def _provision_tool(
        self, tool: CatalogTool, result: ProvisionResult, *, skip_apt: bool
    ) -> None:
        if self._verified(tool):
            return
        steps = [s for s in _steps(tool.install) if not isinstance(s, CustomInstallStep)]
        if not steps and not any(isinstance(s, CustomInstallStep) for s in _steps(tool.install)):
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
            ok, msg = self._apply_step(step, binary=binary, tool_id=tool.id)
            if not ok:
                result.errors.append(f"{tool.id}: {msg}")
                continue   # the next source is the fallback (github_release → pip → git_clone)
            if msg and msg not in result.installed:
                result.installed.append(msg)
            if self._verified(tool):
                break
        if self._verified(tool):
            result.errors = [e for e in result.errors if not e.startswith(f"{tool.id}:")]   # a fallback worked
            return
        if not self._verified(tool) and not any(e.startswith(f"{tool.id}:") for e in result.errors):
            result.errors.append(f"{tool.id}: verify failed after install")

    @staticmethod
    def _cli_present(tool: CatalogTool) -> bool:
        """Same PATH gate as ``InstallResolver`` / ``ProvisionService``."""
        # circular: install → CatalogProvisioner / ToolCatalog
        from orchestrator.tools.install import cli_on_path

        return cli_on_path(tool.binary or tool.id)

    @staticmethod
    def _verified(tool: CatalogTool) -> bool:
        """Ready when CLI is on PATH; YAML ``verify`` still required when present."""
        if not CatalogProvisioner._cli_present(tool):
            return False
        verify_steps = normalize_verify(tool.verify)
        if not verify_steps:
            return True
        return bool(CatalogProvisioner.run_verify(tool).get("ok"))

    @staticmethod
    def run_verify(tool: CatalogTool) -> dict[str, Any]:
        """Run verify commands and return structured output for Learn / logs."""
        steps_out: list[dict[str, Any]] = []
        any_ok = False
        chunks: list[str] = []
        verify_steps = normalize_verify(tool.verify)
        if not verify_steps:
            on_path = CatalogProvisioner._cli_present(tool)
            name = (tool.binary or tool.id or "").strip()
            return {
                "ok": on_path,
                "steps": [],
                "output": (
                    f"(no verify commands — {name or tool.id} "
                    f"{'on PATH' if on_path else 'not on PATH'})"
                ),
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
    def _write_manifest(workspace: Path | None, keys: list[str], result: ProvisionResult) -> None:
        if workspace is None:
            return
        try:
            path = Path(workspace) / ".sandbox-cli-manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"cli_keys": list(keys or []), **result.to_dict()}, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Failed to write tool manifest under %s", workspace, exc_info=True)
