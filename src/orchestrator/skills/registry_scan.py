"""Source-aware recursive SKILL.md discovery + cache fingerprints."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from orchestrator.skills.common import find_manifest
from orchestrator.skills.providers.base import AFFINITY_NAME

logger = logging.getLogger(__name__)

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

_SIDECARS = (AFFINITY_NAME, "skills.lock.yaml")
_SETUP_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
)


@dataclass(frozen=True)
class SkillCandidate:
    """One discovered skill pack root + manifest path."""

    root: Path
    manifest: Path
    source: str  # native | imported | project | personal | plugin | additional
    namespace: str = ""


def _is_skipped_dir(path: Path) -> bool:
    name = path.name
    if name in _SKIP_DIR_NAMES:
        return True
    if name.startswith(".") and name not in {".claude", ".agents", ".agent", ".cursor"}:
        # Allow known agent skill roots; skip other dot-dirs.
        return True
    return False


def discover_skill_candidates(
    skills_dir: Path,
    *,
    source: str = "native",
    max_depth: int = 8,
    skip_helpers: bool = True,
) -> list[SkillCandidate]:
    """Recursively find skill packs under ``skills_dir``.

    A pack is any directory containing ``SKILL.md`` / ``skill.md``. Symlinked
    directories are deduplicated by resolved path. Diagnostics are logged; this
    function never raises for individual path errors.
    """
    root = Path(skills_dir)
    if not root.is_dir():
        return []

    found: list[SkillCandidate] = []
    seen_roots: set[Path] = set()

    def walk(path: Path, depth: int, namespace: str) -> None:
        if depth > max_depth:
            return
        try:
            if not path.is_dir():
                return
        except OSError as exc:
            logger.debug("skill scan skip %s: %s", path, exc)
            return

        # Treat current dir as a pack if it has a manifest.
        manifest = find_manifest(path)

        if manifest is not None:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen_roots:
                return
            seen_roots.add(resolved)
            # Namespace set while descending into ``<ns>/skills/`` (see walk).
            ns = namespace
            found.append(
                SkillCandidate(
                    root=path,
                    manifest=manifest,
                    source=source,
                    namespace=ns,
                )
            )
            # Do not descend into an already-identified skill pack.
            return

        try:
            children = sorted(path.iterdir())
        except OSError as exc:
            logger.debug("skill scan list failed %s: %s", path, exc)
            return

        for child in children:
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if _is_skipped_dir(child):
                continue
            if skip_helpers and child.name == "helpers" and depth == 0:
                continue
            # When entering ``…/<plugin>/skills/``, remember the plugin id.
            next_ns = namespace
            if child.name in {"skills", "skill"} and not namespace:
                next_ns = path.name
            walk(child, depth + 1, next_ns)

    walk(root, 0, "")
    found.sort(key=lambda c: (c.source, c.namespace, c.root.as_posix().lower()))
    return found


def discover_skill_manifests(skills_dir: Path) -> list[Path]:
    """Compat: manifest paths under ``skills_dir`` (native defaults)."""
    return [c.manifest for c in discover_skill_candidates(skills_dir, source="native")]


def skill_tree_fingerprint(root: Path) -> str:
    """Content fingerprint of skill metadata and setup files under ``root``.

    Used for registry cache invalidation. Setup files are included because
    dependency discovery may walk from a nested skill to the provider root.
    """
    base = Path(root)
    if not base.is_dir():
        return "0"
    digest = hashlib.sha256()
    paths: list[Path] = []
    for candidate in discover_skill_candidates(base, source="scan", max_depth=10):
        paths.append(candidate.manifest)
        for sidecar in _SIDECARS:
            side = candidate.root / sidecar
            if side.is_file():
                paths.append(side)
        current = candidate.root
        while True:
            for filename in _SETUP_FILES:
                setup = current / filename
                if setup.is_file():
                    paths.append(setup)
            if current == base or current.parent == current:
                break
            try:
                current = current.parent
                current.relative_to(base)
            except ValueError:
                break
    unique_paths = {path.resolve() for path in paths}
    for path in sorted(unique_paths, key=lambda p: p.as_posix()):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(path.as_posix().encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\n")
    return digest.hexdigest()[:32] or "0"


def skill_tree_mtime(root: Path) -> float:
    """Latest mtime across manifests (any casing) + sidecars."""
    base = Path(root)
    if not base.is_dir():
        return 0.0
    latest = 0.0
    try:
        latest = float(base.stat().st_mtime)
    except OSError:
        latest = 0.0
    for candidate in discover_skill_candidates(base, source="scan", max_depth=10):
        for path in (candidate.manifest, *(candidate.root / s for s in _SIDECARS)):
            try:
                if path.is_file():
                    latest = max(latest, float(path.stat().st_mtime))
            except OSError:
                continue
    return latest


__all__ = [
    "SkillCandidate",
    "discover_skill_candidates",
    "discover_skill_manifests",
    "skill_tree_fingerprint",
    "skill_tree_mtime",
]
