"""Read-only discovery over a fetched marketplace source.

Every scanner here only *reads* files as text (``Path.read_text``,
``json.loads``, YAML/TOML parsing) — nothing from the source is ever
executed, imported as a Python module, or shell-invoked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

_AGENT_EXTS = (".toml", ".yaml", ".yml")
_MCP_MANIFEST_NAMES = {"manifest.json", "mcp.json", ".mcp.json"}
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "htmlcov",
    }
)
_SKILL_MANIFEST_NAMES = ("SKILL.md", "skill.md")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _is_skipped_dir(name: str) -> bool:
    if name in _SKIP_DIR_NAMES:
        return True
    return name.startswith(".")


def _iter_dirs(root: Path, *, max_depth: int = 8) -> list[Path]:
    """Every directory under ``root`` (inclusive), skipping junk/hidden ones."""
    found: list[Path] = [root]
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or _is_skipped_dir(child.name):
                continue
            found.append(child)
            stack.append((child, depth + 1))
    return found


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    match = _FRONTMATTER_RE.match((text or "").strip())
    return (match.group(1), match.group(2) or "") if match else None


def _parse_frontmatter(text: str) -> dict[str, Any]:
    split = _split_frontmatter(text)
    if not split:
        return {}
    try:
        data = yaml.safe_load(split[0])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def scan_skills(root: Path) -> list[dict[str, Any]]:
    """Every ``SKILL.md`` pack found, with its (pre-namespacing) frontmatter."""
    base = Path(root)
    out: list[dict[str, Any]] = []
    for pack_dir in _iter_dirs(base):
        manifest = next(
            (pack_dir / name for name in _SKILL_MANIFEST_NAMES if (pack_dir / name).is_file()),
            None,
        )
        if manifest is None:
            continue
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = str(fm.get("name") or pack_dir.name).strip().lower()
        out.append(
            {
                "name": name,
                "description": str(fm.get("description") or "").strip(),
                "path": str(pack_dir.relative_to(base)),
            }
        )
    return out


def _iter_agent_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _AGENT_EXTS:
            continue
        if "agents" not in {p.lower() for p in path.parent.parts}:
            continue
        found.append(path)
    return found


def scan_agents(root: Path) -> list[dict[str, Any]]:
    """Codex/OpenAI-style subagent configs (``agents/*.toml`` or ``.yaml``).

    These have no automatic mapping onto a host's own agent/role concept;
    the caller stages each as a draft for a human to review.
    """
    import tomllib

    out: list[dict[str, Any]] = []
    for path in _iter_agent_files(root):
        try:
            raw = path.read_text(encoding="utf-8")
            data = tomllib.loads(raw) if path.suffix.lower() == ".toml" else (yaml.safe_load(raw) or {})
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        description = str(data.get("description") or "").strip()
        instructions = str(
            data.get("developer_instructions") or data.get("instructions") or ""
        ).strip()
        if not description and not instructions:
            continue
        name = str(data.get("name") or path.stem).strip().lower()
        out.append(
            {
                "name": name,
                "description": description,
                "instructions": instructions[:4000],
                "path": str(path.relative_to(root)),
            }
        )
    return out


def scan_mcp_pointers(root: Path) -> list[dict[str, Any]]:
    """MCP manifests that *point at* an external server repo rather than
    being runnable server code themselves (no ``command``). A manifest with
    ``command`` is real, runnable server code — ``McpInstaller`` already
    handles that case directly; this scanner only surfaces the pointer so the
    caller can one-click install the repo it names, through that installer.
    """
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.lower() not in _MCP_MANIFEST_NAMES:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("command"):
            continue
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        repo = str(source.get("default") or data.get("repo") or "").strip()
        if not repo:
            continue
        out.append(
            {
                "name": str(data.get("name") or path.parent.name).strip().lower(),
                "repo_url": repo,
                "secrets": [str(s) for s in (data.get("secrets") or [])],
                "configuration": [str(c) for c in (data.get("configuration") or [])],
                "path": str(path.relative_to(root)),
            }
        )
    return out


def scan_all(root: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        "skills": scan_skills(root),
        "agents": scan_agents(root),
        "mcp_pointers": scan_mcp_pointers(root),
    }
