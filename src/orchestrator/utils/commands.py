"""Catalog-driven shell / capability command helpers (no tool-name hardcoding)."""

from __future__ import annotations

import ast
import re
from functools import lru_cache

from orchestrator.capabilities.registry import ensure_registered

_TOKEN_CLEAN = re.compile(r"[^a-z0-9._+/-]+")


@lru_cache(maxsize=1)
def catalog_cli_names() -> frozenset[str]:
    """Lowercase catalog ids + binaries (invalidated via ``clear_command_caches``)."""
    try:
        # circular: ToolCatalog.invalidate → clear_command_caches
        from orchestrator.tools.catalog import ToolCatalog

        names: set[str] = set()
        for tool in ToolCatalog.shared().all().values():
            if getattr(tool, "is_image_tier", False):
                continue
            for raw in (tool.id, tool.binary):
                key = str(raw or "").strip().lower()
                if key:
                    names.add(key)
        return frozenset(names)
    except Exception:
        return frozenset()


@lru_cache(maxsize=1)
def capability_tool_names() -> frozenset[str]:
    """Registered capability + CrewAI tool names (e.g. bash, provision_cli)."""
    names: set[str] = set()
    try:
        names.update(ensure_registered().names())
    except Exception:
        pass
    try:
        from orchestrator.crew.tools import known_tool_names

        names.update(known_tool_names())
    except Exception:
        pass
    return frozenset(n.strip().lower() for n in names if str(n).strip())


@lru_cache(maxsize=1)
def role_allow_binaries() -> frozenset[str]:
    """Union of skill ``allow_binaries`` across the role catalog."""
    try:
        # circular: RoleRegistry.invalidate → clear_command_caches
        from orchestrator.crew.roles.registry import RoleRegistry

        names: set[str] = set()
        for role in RoleRegistry.shared().list_roles():
            for raw in role.allow_binaries or ():
                key = str(raw or "").strip().lower()
                if key:
                    names.add(key)
        return frozenset(names)
    except Exception:
        return frozenset()


def clear_command_caches() -> None:
    catalog_cli_names.cache_clear()
    capability_tool_names.cache_clear()
    role_allow_binaries.cache_clear()


def known_cli_names() -> frozenset[str]:
    """CLIs resolvable from tools/catalog and/or role skill allow lists."""
    return frozenset(catalog_cli_names() | role_allow_binaries())


def catalog_cli_in_command(command: str) -> str:
    """First catalog/role CLI token appearing in a shell line / pipeline."""
    clis = known_cli_names()
    if not clis:
        return ""
    for segment in re.split(r"\s*(?:;|&&|\|\|)\s*", command or ""):
        for tok in segment.strip().split():
            key = _TOKEN_CLEAN.sub("", tok.lower())
            if key in clis:
                return key
    return ""


def _first_token(text: str) -> str:
    first = (text or "").strip().split(None, 1)[0] if (text or "").strip() else ""
    return _TOKEN_CLEAN.sub("", first.lower())


def _call_func_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return (func.id or "").strip().lower()
    if isinstance(func, ast.Attribute):
        return (func.attr or "").strip().lower()
    return ""


def _str_const(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip()
    return ""


def _shell_from_capability_call(text: str) -> str:
    """If ``text`` is one capability Call, return its shell payload (else "").

    Contract:
    - ``command=`` keyword on any registered capability → that string
    - else first positional string on a capability tagged ``shell`` (``bash``)
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        body = ast.parse(raw, mode="eval").body
    except SyntaxError:
        return ""
    if not isinstance(body, ast.Call):
        return ""
    name = _call_func_name(body)
    caps = capability_tool_names()
    if not name or (caps and name not in caps):
        return ""
    for kw in body.keywords:
        if kw.arg == "command":
            val = _str_const(kw.value)
            if val:
                return val
    entry = ensure_registered().get(name)
    if entry is not None and "shell" in entry.tags and body.args:
        return _str_const(body.args[0])
    return ""


def as_shell_cli(text: str) -> str:
    """Return a catalog/role shell argv line, or ``\"\"`` if ``text`` is not one.

    Accepts:
    - plain CLI: ``nmap -sV 10.0.0.1``
    - capability call whose payload is that CLI (AST-parsed, not regex)
    """
    raw = (text or "").strip()
    if not raw or "\n" in raw:
        return ""
    clis = known_cli_names()
    if _first_token(raw) in clis:
        return raw
    extracted = _shell_from_capability_call(raw)
    if extracted and _first_token(extracted) in clis:
        return extracted
    return ""


def looks_like_agent_command(text: str) -> bool:
    """True for a catalog CLI line or a single registered capability call."""
    raw = (text or "").strip()
    if not raw:
        return False
    if "\n" in raw:
        return False
    if as_shell_cli(raw):
        return True
    if _first_token(raw) in known_cli_names():
        return True
    try:
        body = ast.parse(raw, mode="eval").body
    except SyntaxError:
        return False
    if not isinstance(body, ast.Call):
        return False
    name = _call_func_name(body)
    return bool(name and name in capability_tool_names())



def normalize_objective_commands(commands: list | str | None) -> list[str]:
    """Normalize planner/operator command lists: shell CLIs first, then hints.

    Specialist objectives should store plain shell (``nmap …``). Capability
    hints (``roe_status()``, …) are kept when they are not shell wrappers.
    ``bash("nmap …")`` forms are coerced to the inner shell CLI.
    """
    items = [commands] if isinstance(commands, str) else list(commands or [])
    shells: list[str] = []
    hints: list[str] = []
    seen: set[str] = set()
    for raw in items:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        shell = as_shell_cli(text)
        if shell:
            if shell not in seen:
                shells.append(shell)
                seen.add(shell)
            seen.add(text)
            continue
        if looks_like_agent_command(text) or text.endswith(")"):
            hints.append(text)
            seen.add(text)
    return [*shells, *hints][:20]



