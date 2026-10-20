"""Install a folder or zip the operator dropped.

SKILL.md is placed as-is, except its frontmatter ``name:`` is rewritten with
a namespace prefix when the pack was found nested under a ``<plugin>/skills/``
directory (a plugin bundle) — see ``apply_skill_namespace`` for why.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from orchestrator.skills.common import apply_candidate_namespace
from orchestrator.skills.install.place import _frontmatter_name, place_pack
from orchestrator.skills.registry_scan import SkillCandidate, discover_skill_candidates
from orchestrator.utils.install_skip import skip_rel_path
from orchestrator.utils.paths import safe_join
from orchestrator.utils.zip_extract import extract_zip_safe


def _pack_rank(path: Path, name: str) -> tuple[int, int, str]:
    """Prefer ``…/skills/<name>`` over e.g. ``docs/`` with the same frontmatter name."""
    parts = [p.lower() for p in path.parts]
    dir_match = 0 if path.name.lower() == name else 1
    under_skills = 0 if any(p in {"skills", "skill"} for p in parts) else 1
    return (dir_match, under_skills, path.as_posix().lower())


def _unique_candidates(candidates: list[SkillCandidate]) -> list[SkillCandidate]:
    """One pack per (namespace, skill id) — keep the best-ranked directory.

    Two different namespaces (plugin bundles) each keep their own same-named
    pack; within one namespace, duplicates collapse to the best-ranked path.
    """
    best: dict[tuple[str, str], SkillCandidate] = {}
    for candidate in candidates:
        try:
            name = _frontmatter_name(candidate.root)
        except ValueError:
            continue
        key = (candidate.namespace, name)
        prev = best.get(key)
        if prev is None or _pack_rank(candidate.root, name) < _pack_rank(prev.root, name):
            best[key] = candidate
    return [best[key] for key in sorted(best)]


def install_directory(source: Path, *, origin: str = "") -> list[str]:
    root = Path(source)
    candidates = _unique_candidates(
        discover_skill_candidates(root, source="drop", skip_helpers=True)
    )
    if not candidates:
        raise ValueError("dropped folder has no SKILL.md")
    label = origin or f"drop:{root.name}"
    ids: list[str] = []
    for candidate in candidates:
        apply_candidate_namespace(candidate)
        ids.append(place_pack(candidate.root, source=label))
    return ids


def install_zip(data: bytes, *, filename: str = "pack.zip") -> list[str]:
    with tempfile.TemporaryDirectory(prefix="orchestrator-skill-zip-") as tmp:
        dest_root = extract_zip_safe(data, Path(tmp))
        return install_directory(dest_root, origin=f"zip:{filename}")


def install_relative_files(
    files: list[tuple[str, bytes]],
    *,
    origin: str = "drop",
) -> list[str]:
    """Rebuild a dropped folder from ``(relative path, bytes)`` pairs."""
    if not files:
        raise ValueError("no files")
    # Keep ``.claude/skills/...`` trees; only drop junk dirs (venv, node_modules, …).
    kept = [
        (rel, payload)
        for rel, payload in files
        if not skip_rel_path(rel, skip_hidden=False)
    ]
    if not kept:
        raise ValueError(
            "upload contained only ignored paths (.venv, node_modules, …); "
            "choose the skill pack folder"
        )
    with tempfile.TemporaryDirectory(prefix="orchestrator-skill-drop-") as tmp:
        root = Path(tmp)
        for rel, payload in kept:
            target = safe_join(root, rel)
            if target is None:
                raise ValueError(f"unsafe path {rel!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return install_directory(root, origin=origin)
