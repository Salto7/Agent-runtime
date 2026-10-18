"""Safe relative-path joining for pack installs (skills / MCP / marketplace)."""

from __future__ import annotations

from pathlib import Path


def normalize_rel_path(relative_path: str | None) -> str | None:
    """Normalize a pack-relative path; return None if empty or escapes with ``..``."""
    rel = (relative_path or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    return rel


def safe_join(root: Path, relative_path: str | None) -> Path | None:
    """Join ``relative_path`` under ``root``; None if escape or empty."""
    rel = normalize_rel_path(relative_path)
    if rel is None:
        return None
    base = Path(root).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target
