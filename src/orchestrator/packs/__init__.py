"""Shared pack helpers for skill resources (``scripts/`` / ``assets/`` / ``references/``)."""

from __future__ import annotations

from pathlib import Path

from orchestrator.utils.paths import normalize_rel_path, safe_join

SKILL_RESOURCE_ROOTS = ("scripts", "assets", "references")
_RESOLVE_ROOTS = ("scripts", "assets", "references")


def list_resource_files(
    pack_dir: Path, *, roots: tuple[str, ...] | None = None
) -> list[str]:
    """Relative paths under the given resource roots."""
    out: list[str] = []
    root = Path(pack_dir)
    if not root.is_dir():
        return out
    for root_name in roots or SKILL_RESOURCE_ROOTS:
        base = root / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            out.append(path.relative_to(root).as_posix())
    return out


def list_skill_resource_files(pack_dir: Path) -> list[str]:
    return list_resource_files(pack_dir, roots=SKILL_RESOURCE_ROOTS)


def resolve_resource(pack_dir: Path, relative_path: str) -> Path | None:
    """Resolve a pack-relative resource path under scripts/assets/references."""
    rel = normalize_rel_path(relative_path)
    if rel is None:
        return None
    base = Path(pack_dir).resolve()
    candidates = [rel]
    for root_name in _RESOLVE_ROOTS:
        prefix = f"{root_name}/"
        if rel.startswith(prefix):
            candidates.append(f"{root_name}/{rel[len(prefix):]}")
        else:
            candidates.append(f"{root_name}/{rel}")
    if rel.startswith("assets/"):
        candidates.append(f"scripts/{rel.removeprefix('assets/')}")
    if rel.startswith("scripts/"):
        candidates.append(f"assets/{rel.removeprefix('scripts/')}")
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = safe_join(base, candidate)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


__all__ = [
    "SKILL_RESOURCE_ROOTS",
    "list_resource_files",
    "list_skill_resource_files",
    "resolve_resource",
]
